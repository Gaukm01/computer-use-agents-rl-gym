"""Health-check tests.

The bug these exist to prevent: reporting auth healthy because the cookie *file* exists,
while the session behind it is dead. That combination shows a green signal and then errors
every single attempt in a job — the most confusing possible failure, since nothing in the
job output points at setup.

Browser-free: the auth check is a single HTTP request, so these are fast.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from rlgym import service

GYM_URL = "http://localhost:3000"


def gym_is_up() -> bool:
    try:
        with urllib.request.urlopen(f"{GYM_URL}/api/health", timeout=3) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


needs_gym = pytest.mark.skipif(not gym_is_up(), reason="no gym on :3000 — start one with `python -m rlgym.gym_stack --port 3000 --keep`")
needs_auth = pytest.mark.skipif(
    not service.session_token(), reason="no captured session — `--keep` writes one when it provisions"
)


# ----------------------------------------------------------------------------------------
# Reading the cookie out of storage_state
# ----------------------------------------------------------------------------------------


def test_missing_file_is_not_captured(tmp_path) -> None:
    captured, valid, detail = service.check_auth(GYM_URL, tmp_path / "nope.json")
    assert captured is False
    assert valid is False
    assert "capture_auth" in (detail or "")


def test_file_without_session_cookie_is_invalid(tmp_path) -> None:
    """A storage_state can exist and still carry no Metabase session."""
    path = tmp_path / "storage_state.json"
    path.write_text(json.dumps({"cookies": [{"name": "other", "value": "x"}], "origins": []}))

    captured, valid, detail = service.check_auth(GYM_URL, path)
    assert captured is True, "the file does exist"
    assert valid is False, "but it cannot authenticate"
    assert service.SESSION_COOKIE in (detail or "")


def test_malformed_file_does_not_raise(tmp_path) -> None:
    path = tmp_path / "storage_state.json"
    path.write_text("{not json")
    assert service.session_token(path) is None
    captured, valid, _ = service.check_auth(GYM_URL, path)
    assert (captured, valid) == (True, False)


# ----------------------------------------------------------------------------------------
# Actually exercising the session — the point of the fix
# ----------------------------------------------------------------------------------------


@needs_gym
def test_expired_session_reports_invalid_not_healthy(tmp_path) -> None:
    """THE regression test.

    A well-formed cookie file holding a dead session id. Checking only for file existence
    would call this healthy; using the cookie proves it is not.
    """
    path = tmp_path / "storage_state.json"
    path.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": service.SESSION_COOKIE,
                        "value": "00000000-0000-0000-0000-000000000000",
                        "domain": "localhost",
                        "path": "/",
                    }
                ],
                "origins": [],
            }
        )
    )

    captured, valid, detail = service.check_auth(GYM_URL, path)
    assert captured is True, "file exists, so the old check would have said healthy"
    assert valid is False, "but the session is dead"
    assert "expired" in (detail or "").lower()


@needs_gym
@needs_auth
def test_live_session_reports_valid_and_names_the_user() -> None:
    captured, valid, detail = service.check_auth(GYM_URL)
    assert (captured, valid) == (True, True)
    assert "@" in (detail or ""), "should report which account the session belongs to"


def test_health_reports_what_gates_a_run() -> None:
    """No long-lived gym to probe any more -- each attempt provisions its own. Health now
    covers the things whose absence makes every attempt fail."""
    status = service.health()
    assert status.tasks_available == 10
    assert status.max_gyms >= 1
    # can_run is the conjunction of all four gates; detail explains any that are false.
    assert status.can_run == (
        status.docker_available
        and status.images_present
        and status.dump_present
        and status.credentials_configured
    )
    if not status.can_run:
        assert status.detail


# ----------------------------------------------------------------------------------------
# "Cannot determine" is distinct from "broken"
# ----------------------------------------------------------------------------------------


@needs_auth
def test_unreachable_gym_yields_unknown_not_invalid() -> None:
    """With the gym down, the cookie's state is unknowable. Reporting it as invalid would
    send someone to re-run capture_auth when the real problem is that Metabase is off."""
    captured, valid, detail = service.check_auth("http://localhost:59999")
    assert captured is True
    assert valid is None, "must be unknown, not False"
    assert "reach" in (detail or "").lower()


def test_unreachable_gym_is_not_reachable() -> None:
    assert service.gym_reachable("http://localhost:59999", timeout=1.0) is False
