"""Service layer: everything the platform can do, with no web framework in sight.

This is the seam the API will sit on. Nothing here imports FastAPI, so the HTTP layer
becomes pure wiring -- route -> service call -> pydantic model -> JSON -- and the same
functions stay usable from a CLI, a notebook, or a test without spinning up a server.

Jobs run in a background thread and stream their state to `runs/<job_id>/job.json`, so
progress is readable by polling a file. That means a UI can follow a running job without
any websocket plumbing, and a job survives being observed by nobody.

Known limitation, deliberate for now: the registry of *running* jobs lives in memory, so
restarting the API abandons in-flight jobs. Their artifacts and job.json survive, and
`resume_job()` picks them up where they stopped. Moving the registry into a DB or a queue
is the Milestone 3 answer, not something to fake here.
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
from datetime import datetime, timezone
from typing import Any

from . import executor, orchestrator, ports, tasks as tasks_module, vendor
from .schemas import (
    AgentConfig,
    AttemptArtifact,
    EvaluationResult,
    HealthStatus,
    JobResult,
    JobSpec,
    JobStatus,
    Limits,
    Task,
)

DEFAULT_GYM_URL = "http://localhost:3000"
# Metabase's session cookie. The session id it holds is a row in core_session in the app
# database, which is why any restore invalidates it.
SESSION_COOKIE = "metabase.SESSION"


class _RunningJob:
    __slots__ = ("thread", "cancel")

    def __init__(self, thread: threading.Thread, cancel: threading.Event) -> None:
        self.thread = thread
        self.cancel = cancel


_running: dict[str, _RunningJob] = {}
_registry_lock = threading.Lock()


# ======================================================================================
# Health
# ======================================================================================


def gym_reachable(gym_url: str = DEFAULT_GYM_URL, timeout: float = 2.0) -> bool:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"{gym_url.rstrip('/')}/api/health", timeout=timeout
        ) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def session_token(state_path: pathlib.Path | None = None) -> str | None:
    """Pull Metabase's session id out of the saved Playwright storage_state."""
    path = state_path or vendor.STORAGE_STATE
    if not path.is_file():
        return None
    try:
        state = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    for cookie in state.get("cookies", []):
        if cookie.get("name") == SESSION_COOKIE:
            value = cookie.get("value")
            return value if isinstance(value, str) and value else None
    return None


def check_auth(
    gym_url: str = DEFAULT_GYM_URL, state_path: pathlib.Path | None = None
) -> tuple[bool, bool | None, str | None]:
    """Does the stored cookie still authenticate? Returns (captured, valid, detail).

    Verified by *using* the cookie -- `GET /api/user/current` with it attached -- rather
    than by checking the file exists. A one-request check costs ~50ms, where launching a
    browser to try it would cost seconds on an endpoint the UI polls.

    Hitting Metabase's REST API here does not weaken the visual-only rule: that constrains
    the *agent*, not the harness. This is the same category as the grader querying
    Postgres directly -- infrastructure checking itself, never the policy perceiving its
    environment.
    """
    import urllib.error
    import urllib.request

    path = state_path or vendor.STORAGE_STATE
    if not path.is_file():
        return False, False, "no storage_state.json — run `python -m rlgym.capture_auth`"

    token = session_token(path)
    if token is None:
        return (
            True,
            False,
            f"storage_state.json has no {SESSION_COOKIE} cookie — re-run capture_auth",
        )

    request = urllib.request.Request(
        f"{gym_url.rstrip('/')}/api/user/current",
        headers={"Cookie": f"{SESSION_COOKIE}={token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            body = json.loads(response.read() or b"{}")
        who = body.get("email") or body.get("common_name") or "authenticated"
        return True, True, f"session valid ({who})"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return (
                True,
                False,
                "session expired or invalidated — re-run `python -m rlgym.capture_auth`",
            )
        return True, None, f"could not verify session: HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        # Gym unreachable: the cookie's state is unknowable, not bad.
        return True, None, f"could not reach the gym to verify the session ({exc})"


REQUIRED_IMAGES = ("postgres:16.9", "metabase/metabase:v0.54.15")


def health() -> HealthStatus:
    """What gates a run, now that every attempt provisions its own gym.

    Images are checked explicitly because a cold `docker pull` inside an attempt would
    stall provisioning for minutes and read as a hang rather than as setup.
    """
    import subprocess

    missing: list[str] = []

    try:
        docker_ok = (
            subprocess.run(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                timeout=10,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        docker_ok = False
    if not docker_ok:
        missing.append("Docker daemon unreachable (try `colima start`)")

    images_ok = False
    if docker_ok:
        try:
            listed = subprocess.run(
                ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
                capture_output=True,
                text=True,
                timeout=20,
            ).stdout.split()
            absent = [i for i in REQUIRED_IMAGES if i not in listed]
            images_ok = not absent
            if absent:
                missing.append(f"images not pulled: {', '.join(absent)}")
        except (OSError, subprocess.SubprocessError):
            missing.append("could not list Docker images")

    dump_ok = (vendor.PROJECT_ROOT / "resources" / "metabase_envdata.sql").is_file()
    if not dump_ok:
        missing.append("resources/metabase_envdata.sql missing")

    creds_ok = bool(os.environ.get("METABASE_EMAIL") and os.environ.get("METABASE_PASSWORD"))
    if not creds_ok:
        missing.append("METABASE_EMAIL / METABASE_PASSWORD not set in .env")

    try:
        count = len(list_tasks())
    except Exception:  # noqa: BLE001 -- a broken task file shouldn't 500 the health check
        count = 0

    return HealthStatus(
        docker_available=docker_ok,
        images_present=images_ok,
        dump_present=dump_ok,
        credentials_configured=creds_ok,
        tasks_available=count,
        max_gyms=ports.MAX_GYMS,
        detail="; ".join(missing) or None,
    )


# ======================================================================================
# Tasks
# ======================================================================================


def list_tasks(tasks_file: str | pathlib.Path | None = None) -> list[Task]:
    """Every task the platform can run. The UI calls this to render the task picker."""
    return tasks_module.load_tasks(tasks_file)


def get_task(task_id: str, tasks_file: str | pathlib.Path | None = None) -> Task | None:
    return tasks_module.load_task_map(tasks_file).get(task_id)


def save_tasks_file(content: bytes | str, filename: str = "tasks.json") -> tuple[pathlib.Path, list[Task]]:
    """Validate an uploaded task file and store it, returning its path and parsed tasks.

    Written to uploads/ rather than over resources/tasks.json, so the pristine file can
    never be clobbered by an upload. The returned path is what a job's spec records, so
    an uploaded file genuinely drives the run rather than only the display.

    Validation happens before the file is written: a bad upload fails immediately with a
    useful message instead of surfacing later as a confusing job failure.
    """
    text = content.decode("utf-8") if isinstance(content, bytes) else content

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON: {exc}") from exc

    if not isinstance(raw, list):
        raise ValueError(
            f"expected a JSON array of tasks, got {type(raw).__name__}"
        )
    if not raw:
        raise ValueError("task file is empty")

    tasks: list[Task] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"task {i} is {type(entry).__name__}, expected an object")
        missing = [k for k in ("id", "task", "answer") if k not in entry]
        if missing:
            raise ValueError(
                f"task {i} is missing required field(s): {', '.join(missing)}"
            )
        try:
            task = Task.model_validate(entry)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"task {i} ({entry.get('id')!r}) is invalid: {exc}") from exc
        # Deliberately NOT checking that `answer` parses as JSON. A task whose answer is
        # `Q4` or `126` is a perfectly gradeable task, and rejecting the whole file over it
        # would enforce an encoding convention we invented rather than one the task set
        # owes us. Task.expected_output() falls back to the raw string, and the grader
        # compares it on its merits.
        if task.id in seen:
            raise ValueError(f"duplicate task id {task.id!r}")
        seen.add(task.id)
        tasks.append(task)

    vendor.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    stem = pathlib.Path(filename).stem or "tasks"
    safe = "".join(c for c in stem if c.isalnum() or c in "-_")[:40] or "tasks"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = vendor.UPLOADS_DIR / f"{safe}-{stamp}.json"
    path.write_text(text)
    return path, tasks


# ======================================================================================
# Jobs
# ======================================================================================


def submit_job(
    task_ids: list[str] | None = None,
    *,
    attempts_per_task: int = 1,
    concurrency: int = 3,
    agent: AgentConfig | None = None,
    model: str | None = None,
    hint: str | None = None,
    max_steps: int | None = None,
    timeout_s: int | None = None,
    headless: bool = True,
    record_video: bool = True,
    tasks_file: str | pathlib.Path | None = None,
    job_id: str | None = None,
    block: bool = False,
) -> JobResult:
    """Start a job and return immediately with its QUEUED/RUNNING state.

    `task_ids=None` means every task in the file. `block=True` runs inline instead of in a
    thread, which is what tests and the CLI want.

    `agent` carries the whole policy configuration -- model and sampling parameters -- and
    is recorded verbatim in the job spec, so a job's numbers stay attributable to the exact
    policy that produced them. `model`/`hint` are conveniences that override single fields,
    for callers with one thing to change.
    """
    available = tasks_module.load_task_map(tasks_file)
    selected = list(available) if not task_ids else task_ids

    unknown = [t for t in selected if t not in available]
    if unknown:
        raise KeyError(
            f"unknown task id(s): {', '.join(unknown)}. Available: {', '.join(available)}"
        )
    if attempts_per_task < 1:
        raise ValueError("attempts_per_task must be >= 1")
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")

    overrides = {k: v for k, v in {"model": model, "hint": hint}.items() if v is not None}
    agent_config = (agent or AgentConfig()).model_copy(update=overrides)

    defaults = Limits()
    spec = orchestrator.build_spec(
        selected,
        attempts_per_task=attempts_per_task,
        concurrency=concurrency,
        agent_config=agent_config,
        limits=Limits(
            max_steps=max_steps or defaults.max_steps,
            timeout_s=timeout_s or defaults.timeout_s,
        ),
        headless=headless,
        record_video=record_video,
        tasks_file=str(tasks_file) if tasks_file else None,
        job_id=job_id,
    )

    if block:
        return orchestrator.run_job(spec, resume=True)

    return _start_background(spec, resume=True)


def resume_job(job_id: str, *, block: bool = False) -> JobResult:
    """Continue a job that was interrupted. Attempts already in a terminal state are
    skipped, so this costs nothing for work already done."""
    existing = orchestrator.read_job(job_id)
    if existing is None:
        raise KeyError(f"unknown job {job_id!r}")
    if is_running(job_id):
        return existing
    if block:
        return orchestrator.run_job(existing.spec, resume=True)
    return _start_background(existing.spec, resume=True)


def _start_background(spec: JobSpec, *, resume: bool) -> JobResult:
    cancel = threading.Event()

    def target() -> None:
        try:
            orchestrator.run_job(spec, resume=resume, cancel=cancel)
        finally:
            with _registry_lock:
                _running.pop(spec.job_id, None)

    thread = threading.Thread(
        target=target, name=f"job-{spec.job_id}", daemon=True
    )
    with _registry_lock:
        _running[spec.job_id] = _RunningJob(thread, cancel)
    thread.start()

    # Give run_job a moment to write the initial job.json so the caller gets real state
    # rather than a placeholder it has to poll for.
    thread.join(timeout=1.0)
    return orchestrator.read_job(spec.job_id) or JobResult(
        spec=spec, status=JobStatus.QUEUED
    )


def get_job(job_id: str) -> JobResult | None:
    return orchestrator.read_job(job_id)


def list_jobs(limit: int | None = None) -> list[JobResult]:
    """Newest first. Reads job.json files -- no index to keep in sync."""
    if not vendor.RUNS_DIR.is_dir():
        return []
    jobs: list[JobResult] = []
    for path in vendor.RUNS_DIR.iterdir():
        if not path.is_dir() or not (path / orchestrator.JOB_FILE).is_file():
            continue
        job = orchestrator.read_job(path.name)
        if job is not None:
            jobs.append(job)
    jobs.sort(key=lambda j: j.spec.created_at or j.started_at, reverse=True)
    return jobs[:limit] if limit else jobs


def is_running(job_id: str) -> bool:
    with _registry_lock:
        entry = _running.get(job_id)
    return bool(entry and entry.thread.is_alive())


def cancel_job(job_id: str) -> bool:
    """Stop scheduling new attempts. Running attempts finish so their artifacts and
    videos are written rather than truncated."""
    with _registry_lock:
        entry = _running.get(job_id)
    if entry is None:
        return False
    entry.cancel.set()
    return True


# ======================================================================================
# Attempts
# ======================================================================================


def get_attempt(job_id: str, attempt_id: str) -> dict[str, Any] | None:
    """Full trajectory for one attempt: artifact + verdict + media paths.

    This is what the transcript viewer renders -- step-by-step actions, the model's
    reasoning, screenshots and the video, alongside expected vs actual.
    """
    out_dir = executor.attempt_dir(attempt_id, job_id)
    artifact = executor.read_artifact(out_dir)
    if artifact is None:
        return None

    evaluation: EvaluationResult | None = None
    eval_path = out_dir / executor.EVALUATION_NAME
    if eval_path.is_file():
        try:
            evaluation = EvaluationResult.model_validate_json(eval_path.read_text())
        except Exception:
            evaluation = None

    return {
        "artifact": artifact,
        "evaluation": evaluation,
        "screenshots": attempt_screenshots(job_id, attempt_id),
        "video": attempt_video(job_id, attempt_id),
    }


def attempt_screenshots(job_id: str, attempt_id: str) -> list[str]:
    """Step screenshots in step order, as paths relative to the attempt directory."""
    steps_dir = executor.attempt_dir(attempt_id, job_id) / executor.STEPS_DIR
    if not steps_dir.is_dir():
        return []
    return [f"{executor.STEPS_DIR}/{p.name}" for p in sorted(steps_dir.glob("*.png"))]


def attempt_video(job_id: str, attempt_id: str) -> str | None:
    video_dir = executor.attempt_dir(attempt_id, job_id) / executor.VIDEO_DIR
    if not video_dir.is_dir():
        return None
    videos = sorted(video_dir.glob("*.webm"))
    return f"{executor.VIDEO_DIR}/{videos[0].name}" if videos else None


def attempt_file(job_id: str, attempt_id: str, relative: str) -> pathlib.Path | None:
    """Resolve a media path for the HTTP layer to serve.

    Path traversal is checked here rather than at the route, so every caller gets the
    protection -- a relative path is only returned if it genuinely resolves inside the
    attempt's own directory.
    """
    base = executor.attempt_dir(attempt_id, job_id).resolve()
    target = (base / relative).resolve()
    if not target.is_file() or base not in target.parents:
        return None
    return target
