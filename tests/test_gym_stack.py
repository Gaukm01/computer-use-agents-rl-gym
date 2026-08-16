"""Ephemeral-gym tests.

Docker is stubbed for everything except the two tests marked `needs_docker`, so the
structural guarantees are checked in milliseconds. The one that matters most is that the
stack is torn down even when provisioning raises -- a leaked stack holds ~1.1 GiB and its
port until someone notices.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time

import pytest

from rlgym import gym_stack, ports, service
from rlgym.schemas import AttemptPhase


def fake_result(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ----------------------------------------------------------------------------------------
# Project naming -- uploaded task ids are not trustworthy input
# ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "attempt_id",
    [
        "problem10-a0-3a1e705b",     # the normal shape
        "Problem_One-a0-deadbeef",   # uppercase + underscore from an uploaded tasks.json
        "my task #1-a0-cafe",        # spaces and punctuation
        "---",                       # degenerate
    ],
)
def test_project_name_is_always_a_legal_compose_name(attempt_id: str) -> None:
    """Compose requires [a-z0-9][a-z0-9_-]*. Task ids come from an uploaded file, so they
    cannot be assumed lowercase or alphanumeric."""
    import re

    name = gym_stack.project_name(attempt_id)
    assert re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name), name
    assert name.startswith(gym_stack.PROJECT_PREFIX.rstrip("-")) or name


def test_project_names_stay_distinct_per_attempt() -> None:
    a = gym_stack.project_name("problem1-a0-aaaaaaaa")
    b = gym_stack.project_name("problem1-a1-bbbbbbbb")
    assert a != b, "two attempts must not share a compose project"


# ----------------------------------------------------------------------------------------
# Port allocation -- the queue IS the concurrency bound
# ----------------------------------------------------------------------------------------


def test_claim_returns_the_port_even_when_the_body_raises() -> None:
    before = ports.available()
    with pytest.raises(RuntimeError):
        with ports.claim():
            raise RuntimeError("provisioning blew up")
    assert ports.available() == before, "a failed attempt leaked its port"


def test_claim_bounds_concurrency() -> None:
    """Holding every port must block the next claim -- that is what caps parallel gyms."""
    held = [ports.claim() for _ in range(ports.MAX_GYMS)]
    for h in held:
        h.__enter__()
    try:
        with pytest.raises(Exception):  # queue.Empty
            with ports.claim(timeout=0.2):
                pass
    finally:
        for h in held:
            h.__exit__(None, None, None)
    assert ports.available() == ports.MAX_GYMS


def test_claimed_ports_are_unique() -> None:
    with ports.claim() as a, ports.claim() as b:
        assert a != b


# ----------------------------------------------------------------------------------------
# Teardown -- the property most likely to regress
# ----------------------------------------------------------------------------------------


def test_stack_is_torn_down_when_provisioning_fails(tmp_path, monkeypatch) -> None:
    """The `finally` must run on the failure path, or a broken attempt leaks a container."""
    calls: list[tuple[str, ...]] = []

    def fake_compose(project, *args, port=None, timeout=None):
        calls.append(args)
        if args[:2] == ("up", "-d"):
            return fake_result(returncode=1, stderr="no space left on device")
        return fake_result()

    monkeypatch.setattr(gym_stack, "_compose", fake_compose)

    phases: list[AttemptPhase] = []
    with pytest.raises(gym_stack.GymProvisionError, match="no space left"):
        with gym_stack.ephemeral_gym(
            "t-a0-dead",
            storage_state_path=tmp_path / "s.json",
            on_phase=phases.append,
        ):
            pass  # pragma: no cover -- never reached

    assert ("down", "-v", "--remove-orphans") in calls, "stack was NOT torn down"
    assert phases == [AttemptPhase.PROVISIONING]


def test_teardown_runs_when_the_body_raises(tmp_path, monkeypatch) -> None:
    """A crash *inside* the attempt must still destroy the gym."""
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(
        gym_stack, "_compose", lambda project, *a, **k: (calls.append(a), fake_result())[1]
    )
    monkeypatch.setattr(gym_stack, "_wait_postgres_healthy", lambda *a, **k: None)
    monkeypatch.setattr(gym_stack, "_restore_dump", lambda *a, **k: None)
    monkeypatch.setattr(gym_stack, "_wait_metabase_ready", lambda *a, **k: None)
    monkeypatch.setattr(gym_stack, "mint_session", lambda url: "tok")
    monkeypatch.setattr(service, "check_auth", lambda *a, **k: (True, True, "ok"))
    monkeypatch.setattr(gym_stack, "_assert_pristine", lambda *a, **k: {"recent_views": 73})

    with pytest.raises(ZeroDivisionError):
        with gym_stack.ephemeral_gym("t-a0-beef", storage_state_path=tmp_path / "s.json"):
            1 / 0

    assert ("down", "-v", "--remove-orphans") in calls


def test_pristine_mismatch_refuses_to_start_the_attempt(tmp_path, monkeypatch) -> None:
    """A gym that isn't seeded correctly must fail loudly, not run and produce a result."""
    monkeypatch.setattr(
        gym_stack,
        "_compose",
        lambda project, *a, **k: fake_result(stdout="99|86" if a[0] == "exec" else ""),
    )
    with pytest.raises(gym_stack.GymProvisionError, match="not pristine"):
        gym_stack._assert_pristine("proj", 3001)


# ----------------------------------------------------------------------------------------
# The minted session must be usable by Playwright AND readable by our own code
# ----------------------------------------------------------------------------------------


def test_storage_state_shape_round_trips(tmp_path) -> None:
    path = gym_stack.write_storage_state("abc-123", tmp_path / "storage_state.json")
    state = json.loads(path.read_text())

    cookie = state["cookies"][0]
    assert cookie["name"] == service.SESSION_COOKIE
    # Host-only domain: cookies ignore port, so one shape works for every stack.
    assert cookie["domain"] == "localhost"
    assert {"path", "expires", "httpOnly", "secure", "sameSite"} <= cookie.keys()
    assert state["origins"] == []

    # The same file our own auth check reads.
    assert service.session_token(path) == "abc-123"


def test_mint_session_without_credentials_is_an_auth_error(monkeypatch) -> None:
    monkeypatch.delenv("METABASE_EMAIL", raising=False)
    monkeypatch.delenv("METABASE_PASSWORD", raising=False)
    with pytest.raises(gym_stack.GymAuthError, match="METABASE_EMAIL"):
        gym_stack.mint_session("http://localhost:3001")


# ----------------------------------------------------------------------------------------
# Teardown must be VERIFIED, not assumed -- the actual cause of the production leak
# ----------------------------------------------------------------------------------------


def test_failed_teardown_is_detected_and_reported(monkeypatch) -> None:
    """The original bug: `compose down -v` ran, failed, and its exit code was ignored, so
    six containers survived and nothing noticed. The port then returned to the pool still
    bound, which is how one bad attempt poisons every later one."""
    monkeypatch.setattr(
        gym_stack, "_compose", lambda *a, **k: fake_result(returncode=1, stderr="i/o error")
    )
    # Containers stubbornly remain, however hard we try.
    monkeypatch.setattr(gym_stack, "_project_containers", lambda project: ["abc123"])
    monkeypatch.setattr(gym_stack, "_run", lambda *a, **k: fake_result(returncode=1, stderr="i/o error"))

    error = gym_stack._teardown("rlgym-t-a0-dead", 3001)
    assert error is not None, "a failed teardown must not report success"
    assert "teardown incomplete" in error
    assert "3001" in error, "the port matters -- it is what the next attempt will collide on"


def test_successful_teardown_reports_no_error(monkeypatch) -> None:
    monkeypatch.setattr(gym_stack, "_compose", lambda *a, **k: fake_result())
    monkeypatch.setattr(gym_stack, "_project_containers", lambda project: [])
    monkeypatch.setattr(gym_stack, "_run", lambda *a, **k: fake_result(stdout=""))
    assert gym_stack._teardown("rlgym-t-a0-beef", 3001) is None


def test_teardown_escalates_to_force_remove(monkeypatch) -> None:
    """compose down failing is not the end -- go at the containers directly."""
    calls: list[list[str]] = []
    monkeypatch.setattr(gym_stack, "_compose", lambda *a, **k: fake_result(returncode=1))
    seen = {"n": 0}

    def containers(project):
        seen["n"] += 1
        return ["abc123"] if seen["n"] <= 3 else []   # gone once force-removed

    monkeypatch.setattr(gym_stack, "_project_containers", containers)
    monkeypatch.setattr(
        gym_stack, "_run", lambda args, **k: (calls.append(args), fake_result(stdout=""))[1]
    )

    error = gym_stack._teardown("rlgym-t-a0-cafe", 3002)
    assert any(a[:3] == ["docker", "rm", "-f"] for a in calls), "never tried a force remove"
    assert error is None


def test_teardown_failure_does_not_mask_the_attempt_result(tmp_path, monkeypatch) -> None:
    """A leaked stack is an operational problem, not the agent's -- the attempt's own
    outcome must still surface."""
    monkeypatch.setattr(gym_stack, "_wait_postgres_healthy", lambda *a, **k: None)
    monkeypatch.setattr(gym_stack, "_restore_dump", lambda *a, **k: None)
    monkeypatch.setattr(gym_stack, "_wait_metabase_ready", lambda *a, **k: None)
    monkeypatch.setattr(gym_stack, "mint_session", lambda url: "tok")
    monkeypatch.setattr(service, "check_auth", lambda *a, **k: (True, True, "ok"))
    monkeypatch.setattr(gym_stack, "_assert_pristine", lambda *a, **k: {"recent_views": 73})
    monkeypatch.setattr(gym_stack, "_compose", lambda *a, **k: fake_result())
    monkeypatch.setattr(gym_stack, "_teardown", lambda p, port: "teardown incomplete: 1 container")

    reported: list[str] = []
    with gym_stack.ephemeral_gym(
        "t-a0-feed",
        storage_state_path=tmp_path / "s.json",
        on_teardown_error=reported.append,
    ) as (url, _state):
        assert url.startswith("http://localhost:")

    assert reported and "teardown incomplete" in reported[0]
