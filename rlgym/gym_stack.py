"""One ephemeral Metabase + Postgres stack per attempt.

Replaces the single long-lived gym. Each attempt claims a port, boots its own stack, restores
the dump, mints its own session, runs, and destroys the stack. Nothing is shared, so nothing
carries over -- which is the organizers' requirement that every attempt "start from the same
point".

**Teardown lives in the caller's process, wrapping the worker subprocess.** On timeout the
executor does `os.killpg(SIGKILL)`, and SIGKILL runs no `finally` -- so provisioning and
teardown must never live inside the worker. The worker is handed a URL and a storage_state
path and knows nothing about Docker.

**H2 isolation is free.** `/plugins` is deliberately not volume-mounted, so the sample
database lives in the container's writable layer and every fresh container extracts a
pristine copy from the image.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from typing import Callable, Iterator

from dotenv import load_dotenv

from . import ports, vendor
from .schemas import AttemptPhase

load_dotenv(vendor.PROJECT_ROOT / ".env")

COMPOSE_FILE = vendor.PROJECT_ROOT / "metabase" / "docker-compose.yml"
DUMP_IN_CONTAINER = "/resources/metabase_envdata.sql"
EPHEMERAL_LABEL = "rlgym.ephemeral"
PROJECT_PREFIX = "rlgym-"

# Generous: three JVMs starting on four vCPUs is markedly slower than the ~30-60s a single
# cold boot takes. These bound a hang, they are not expected durations.
POSTGRES_READY_TIMEOUT_S = 120
RESTORE_TIMEOUT_S = 300
METABASE_READY_TIMEOUT_S = 420
DOCKER_CMD_TIMEOUT_S = 600

# Row counts in resources/metabase_envdata.sql. Asserted before an attempt is allowed to
# start; see the note in _assert_pristine about which tables are safe to check.
PRISTINE_RECENT_VIEWS = 73
PRISTINE_REPORT_CARD = 86

PhaseCallback = Callable[[AttemptPhase], None]


class GymProvisionError(RuntimeError):
    """Docker, the restore, or Metabase's boot failed. Operator action: infrastructure."""


class GymAuthError(RuntimeError):
    """The stack came up but no usable session could be minted. Operator action: credentials."""


# ======================================================================================
# Shelling out
# ======================================================================================


def _run(
    args: list[str], *, timeout: int = DOCKER_CMD_TIMEOUT_S, env: dict | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(vendor.PROJECT_ROOT),
        env={**os.environ, **(env or {})},
    )


def _compose(project: str, *args: str, port: int | None = None, timeout: int = DOCKER_CMD_TIMEOUT_S):
    """`docker compose -p <project> -f metabase/docker-compose.yml …`

    MB_HOST_PORT is required by the compose file even for `down`, since interpolation happens
    before the subcommand runs.
    """
    env = {"MB_HOST_PORT": str(port if port is not None else 0)}
    return _run(
        ["docker", "compose", "-p", project, "-f", str(COMPOSE_FILE), *args],
        timeout=timeout,
        env=env,
    )


def project_name(attempt_id: str) -> str:
    """Compose project names must be [a-z0-9][a-z0-9_-]*.

    Attempt ids embed the task id, which comes from an uploaded tasks.json and is therefore
    not guaranteed to be lowercase or alphanumeric -- sanitize rather than trust it.
    """
    slug = re.sub(r"[^a-z0-9_-]+", "-", f"{PROJECT_PREFIX}{attempt_id}".lower())
    return slug.strip("-_")[:60] or f"{PROJECT_PREFIX}attempt"


# ======================================================================================
# Provisioning steps
# ======================================================================================


def _wait_postgres_healthy(project: str, port: int) -> None:
    deadline = time.monotonic() + POSTGRES_READY_TIMEOUT_S
    container = ""
    while time.monotonic() < deadline:
        found = _compose(project, "ps", "-q", "postgres", port=port, timeout=60)
        container = (found.stdout or "").strip().splitlines()[0] if found.stdout.strip() else ""
        if container:
            state = _run(
                ["docker", "inspect", "-f", "{{.State.Health.Status}}", container], timeout=60
            )
            if state.stdout.strip() == "healthy":
                return
        time.sleep(2)
    raise GymProvisionError(
        f"postgres did not become healthy within {POSTGRES_READY_TIMEOUT_S}s "
        f"(container={container or 'not created'})"
    )


def _restore_dump(project: str, port: int) -> None:
    """Restore straight into metabase_app -- POSTGRES_DB already created it empty.

    Runs before Metabase is started at all, so there is no schema to collide with.
    """
    result = _compose(
        project,
        "exec", "-T", "postgres",
        "pg_restore", "-U", "postgres", "-d", "metabase_app", DUMP_IN_CONTAINER,
        port=port,
        timeout=RESTORE_TIMEOUT_S,
    )
    if result.returncode != 0:
        # A partially restored database is wrong ground truth presented as a valid run.
        raise GymProvisionError(
            f"pg_restore exited {result.returncode}: {(result.stderr or '').strip()[-800:]}"
        )


def _wait_metabase_ready(gym_url: str) -> None:
    deadline = time.monotonic() + METABASE_READY_TIMEOUT_S
    last = "no response"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{gym_url}/api/health", timeout=5) as response:
                if response.status == 200:
                    return
                last = f"HTTP {response.status}"
        except (urllib.error.URLError, OSError) as exc:
            last = str(exc)
        time.sleep(3)
    raise GymProvisionError(
        f"metabase did not answer /api/health within {METABASE_READY_TIMEOUT_S}s ({last})"
    )


def mint_session(gym_url: str) -> str:
    """Create a Metabase session over HTTP and return its id.

    Deliberately NOT `capture_auth.capture()`: that drives Playwright's *sync* API, and this
    runs inside ThreadPoolExecutor threads, where several sync Playwright instances in one
    process fail outright -- the same constraint that makes attempts subprocesses. This is
    thread-safe, ~50ms instead of ~3s, needs no browser, and still keeps the password out of
    the model's context.
    """
    email = os.environ.get("METABASE_EMAIL")
    password = os.environ.get("METABASE_PASSWORD")
    if not email or not password:
        raise GymAuthError("METABASE_EMAIL / METABASE_PASSWORD missing from .env")

    payload = json.dumps({"username": email, "password": password}).encode()
    request = urllib.request.Request(
        f"{gym_url}/api/session",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = (exc.read() or b"").decode(errors="replace")[:300]
        raise GymAuthError(f"POST /api/session failed: HTTP {exc.code} {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise GymAuthError(f"POST /api/session unreachable: {exc}") from exc

    token = body.get("id")
    if not token:
        raise GymAuthError(f"/api/session returned no session id: {body}")
    return token


def write_storage_state(token: str, path: pathlib.Path) -> pathlib.Path:
    """Playwright storage_state carrying just the Metabase session cookie.

    The cookie domain is host-only ("localhost"); cookies ignore port, so one shape works for
    every stack regardless of which port it claimed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "metabase.SESSION",
                        "value": token,
                        "domain": "localhost",
                        "path": "/",
                        "expires": -1,
                        "httpOnly": True,
                        "secure": False,
                        "sameSite": "Lax",
                    }
                ],
                "origins": [],
            },
            indent=2,
        )
    )
    return path


def _project_containers(project: str) -> list[str]:
    """Container ids still belonging to this compose project. The ground truth for whether
    teardown actually happened -- an exit code is not."""
    found = _run(
        ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"],
        timeout=60,
    )
    return found.stdout.split()


def _teardown(project: str, port: int) -> str | None:
    """Destroy the stack and *verify* it is gone. Returns an error string if it is not.

    The original version fired `compose down -v` and ignored the result. When the host disk
    filled, `down` failed, six containers survived, and nothing noticed -- and because the
    port returns to the pool regardless, the next attempt to claim it would have hit
    "port already allocated". Silent teardown failure is how one bad attempt poisons a job.

    Escalates: down -> down again -> force-remove by label -> report.
    """
    attempts = [
        ("down", lambda: _compose(project, "down", "-v", "--remove-orphans", port=port, timeout=180)),
        # A retry genuinely helps: under load the daemon can be transiently busy.
        ("down (retry)", lambda: _compose(project, "down", "-v", "--remove-orphans", port=port, timeout=180)),
    ]
    last_error = ""
    for _label, action in attempts:
        result = action()
        if result.returncode != 0:
            last_error = (result.stderr or "").strip()[-300:]
        if not _project_containers(project):
            break
    else:
        pass

    remaining = _project_containers(project)
    if remaining:
        # Compose could not do it; go at the containers directly.
        forced = _run(["docker", "rm", "-f", *remaining], timeout=180)
        if forced.returncode != 0:
            last_error = (forced.stderr or "").strip()[-300:] or last_error

    # Volumes are named <project>_pgdata and are NOT removed by `docker rm`.
    volumes = _run(["docker", "volume", "ls", "-q"], timeout=60)
    stale = [v for v in volumes.stdout.split() if v.startswith(f"{project}_")]
    if stale:
        _run(["docker", "volume", "rm", "-f", *stale], timeout=180)

    still_there = _project_containers(project)
    leftover_volumes = [
        v for v in _run(["docker", "volume", "ls", "-q"], timeout=60).stdout.split()
        if v.startswith(f"{project}_")
    ]
    if still_there or leftover_volumes:
        return (
            f"teardown incomplete: {len(still_there)} container(s), "
            f"{len(leftover_volumes)} volume(s) remain on port {port}. {last_error}".strip()
        )
    return None


def _assert_pristine(project: str, port: int) -> dict[str, int]:
    """Prove the attempt starts from the seeded state, rather than assuming it.

    Only tables that Metabase does not touch during its own boot are checked.
    `query_execution` and `task_history` are written by Metabase's startup sync, and
    `metabase_fieldvalues` may be too, so none of them can be asserted equal to the dump.
    """
    sql = "SELECT (SELECT count(*) FROM recent_views), (SELECT count(*) FROM report_card)"
    result = _compose(
        project,
        "exec", "-T", "postgres",
        "psql", "-U", "postgres", "-d", "metabase_app", "-tAc", sql,
        port=port,
        timeout=60,
    )
    if result.returncode != 0:
        raise GymProvisionError(f"pristine check failed: {(result.stderr or '').strip()[-400:]}")
    try:
        recent_views, report_card = (int(x) for x in result.stdout.strip().split("|"))
    except ValueError as exc:
        raise GymProvisionError(f"could not parse pristine counts: {result.stdout!r}") from exc

    if recent_views != PRISTINE_RECENT_VIEWS or report_card != PRISTINE_REPORT_CARD:
        raise GymProvisionError(
            "gym is not pristine: "
            f"recent_views={recent_views} (expected {PRISTINE_RECENT_VIEWS}), "
            f"report_card={report_card} (expected {PRISTINE_REPORT_CARD})"
        )
    return {"recent_views": recent_views, "report_card": report_card}


# ======================================================================================
# The context manager
# ======================================================================================


@contextmanager
def ephemeral_gym(
    attempt_id: str,
    *,
    storage_state_path: pathlib.Path,
    on_phase: PhaseCallback | None = None,
    on_pristine: Callable[[dict[str, int]], None] | None = None,
    on_teardown_error: Callable[[str], None] | None = None,
) -> Iterator[tuple[str, pathlib.Path]]:
    """Yield (gym_url, storage_state_path) for a stack that exists only inside this block."""

    def phase(value: AttemptPhase) -> None:
        if on_phase:
            on_phase(value)

    with ports.claim() as port:
        project = project_name(attempt_id)
        gym_url = f"http://localhost:{port}"
        try:
            phase(AttemptPhase.PROVISIONING)

            # Postgres ONLY. `up -d` (no service) would start Metabase the instant Postgres
            # went healthy -- depends_on: service_healthy -- and it would run Liquibase
            # against the empty database, creating a schema that collides with the restore.
            started = _compose(project, "up", "-d", "postgres", port=port)
            if started.returncode != 0:
                raise GymProvisionError(
                    f"compose up postgres failed: {(started.stderr or '').strip()[-800:]}"
                )
            _wait_postgres_healthy(project, port)
            _restore_dump(project, port)

            started = _compose(project, "up", "-d", "metabase", port=port)
            if started.returncode != 0:
                raise GymProvisionError(
                    f"compose up metabase failed: {(started.stderr or '').strip()[-800:]}"
                )
            _wait_metabase_ready(gym_url)

            phase(AttemptPhase.AUTHENTICATING)
            token = mint_session(gym_url)
            write_storage_state(token, storage_state_path)

            # Imported here rather than at module scope: service imports executor, which
            # imports this module, so a top-level import would close a cycle.
            from . import service

            _captured, valid, detail = service.check_auth(gym_url, storage_state_path)
            if valid is not True:
                raise GymAuthError(f"minted session did not authenticate: {detail}")

            counts = _assert_pristine(project, port)
            if on_pristine:
                on_pristine(counts)

            yield gym_url, storage_state_path
        finally:
            # Unconditional AND verified: a leaked stack holds ~1.1 GiB and keeps its port
            # bound, so the next attempt that claims that port cannot start.
            try:
                teardown_error = _teardown(project, port)
            except Exception as exc:  # noqa: BLE001 -- never mask the attempt's own outcome
                teardown_error = f"teardown raised {type(exc).__name__}: {exc}"
            if teardown_error:
                # Reported, never raised: the attempt's real result must survive.
                if on_teardown_error:
                    on_teardown_error(teardown_error)
                else:
                    print(f"[gym_stack] {teardown_error}", flush=True)


# ======================================================================================
# Orphan cleanup
# ======================================================================================


def sweep() -> tuple[int, int, str | None]:
    """Force-remove every ephemeral container and its volumes.

    Returns (found, removed, error). Reporting both matters: when the Docker daemon is
    degraded -- e.g. after the host filled and corrupted containerd's metadata -- `docker rm`
    fails while still exiting quietly enough to look successful. An earlier version returned
    the number *found* and ignored the exit code, so it cheerfully reported "swept 6" having
    removed none.

    Called once at the top of a job. Prevents "port already allocated" on the run after a
    Ctrl-C or a killed API server.

    NOTE: this removes *all* rlgym.ephemeral containers, so starting a second job while one
    is running will destroy the first job's gyms. Accepted deliberately -- jobs are expected
    to run one at a time.
    """
    found = _run(["docker", "ps", "-aq", "--filter", f"label={EPHEMERAL_LABEL}"], timeout=60)
    container_ids = found.stdout.split()
    error: str | None = None

    if container_ids:
        removed = _run(["docker", "rm", "-f", *container_ids], timeout=180)
        if removed.returncode != 0:
            error = (removed.stderr or "").strip()[-400:] or "docker rm failed"

    # `docker rm` leaves named volumes behind; without this they accumulate ~50 MB per
    # orphaned attempt.
    volumes = _run(["docker", "volume", "ls", "-q"], timeout=60)
    stale = [v for v in volumes.stdout.split() if v.startswith(PROJECT_PREFIX)]
    if stale:
        dropped = _run(["docker", "volume", "rm", "-f", *stale], timeout=180)
        if dropped.returncode != 0 and error is None:
            error = (dropped.stderr or "").strip()[-400:] or "docker volume rm failed"

    # Recount rather than trust the exit codes.
    still = _run(["docker", "ps", "-aq", "--filter", f"label={EPHEMERAL_LABEL}"], timeout=60)
    remaining = len(still.stdout.split())
    return len(container_ids), len(container_ids) - remaining, error


# ======================================================================================
# Dev entrypoint -- replaces the old long-lived :3000 gym
# ======================================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Provision a seeded Metabase stack for manual use."
    )
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument(
        "--keep", action="store_true", help="Stay up until Ctrl-C (otherwise tear down at once)."
    )
    args = parser.parse_args()

    state_path = vendor.PROJECT_ROOT / "agent" / "storage_state.json"
    project = project_name(f"dev-{args.port}")
    gym_url = f"http://localhost:{args.port}"

    print(f"provisioning {gym_url} (project {project}) ...")
    try:
        started = _compose(project, "up", "-d", "postgres", port=args.port)
        if started.returncode != 0:
            raise GymProvisionError(started.stderr)
        _wait_postgres_healthy(project, args.port)
        print("  postgres healthy, restoring dump ...")
        _restore_dump(project, args.port)
        started = _compose(project, "up", "-d", "metabase", port=args.port)
        if started.returncode != 0:
            raise GymProvisionError(started.stderr)
        print("  metabase starting (this takes a while) ...")
        _wait_metabase_ready(gym_url)
        write_storage_state(mint_session(gym_url), state_path)
        counts = _assert_pristine(project, args.port)
        print(f"  ready: {gym_url}  pristine={counts}  storage_state={state_path}")

        if args.keep:
            print("  Ctrl-C to tear down.")
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                print("\n  tearing down ...")
    except (GymProvisionError, GymAuthError) as exc:
        print(f"FAILED: {exc}")
        _compose(project, "down", "-v", "--remove-orphans", port=args.port, timeout=180)
        return 1

    _compose(project, "down", "-v", "--remove-orphans", port=args.port, timeout=180)
    print("  done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
