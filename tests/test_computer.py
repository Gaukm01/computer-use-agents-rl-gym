"""Environment-boundary tests.

The pure origin logic runs anywhere. The browser tests need a live gym and a captured auth
cookie, and skip cleanly without them so `pytest tests/` stays green on a fresh checkout.

    colima start && (cd metabase && docker compose up -d)
    agent/.venv/bin/python -m rlgym.capture_auth
    agent/.venv/bin/python -m pytest tests/ -v
"""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from rlgym.computer import ActionRefused, GymComputer, same_origin
from rlgym.vendor import STORAGE_STATE

GYM_URL = "http://localhost:3000"


def gym_is_up() -> bool:
    try:
        with urllib.request.urlopen(f"{GYM_URL}/api/health", timeout=3) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


needs_gym = pytest.mark.skipif(
    not gym_is_up(), reason="gym not running on localhost:3000"
)
needs_auth = pytest.mark.skipif(
    not STORAGE_STATE.is_file(),
    reason="no storage_state.json -- run python -m rlgym.capture_auth",
)


# ----------------------------------------------------------------------------------------
# Origin logic (no browser needed)
# ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,allowed",
    [
        ("http://localhost:3000", True),
        ("http://localhost:3000/question/notebook", True),
        ("/dashboard/2", True),  # relative -- resolves against current origin
        ("about:blank", True),
        ("https://www.google.com", False),
        ("http://localhost:3001", False),  # different port is a different origin
        ("https://localhost:3000", False),  # different scheme too
        ("http://evil.test/metabase", False),
    ],
)
def test_same_origin(url: str, allowed: bool) -> None:
    assert same_origin(url, GYM_URL) is allowed


# ----------------------------------------------------------------------------------------
# Live browser behaviour
# ----------------------------------------------------------------------------------------


@needs_gym
@needs_auth
def test_starts_authenticated_via_storage_state() -> None:
    """Pre-auth means an attempt never spends budget on the login form."""
    with GymComputer(
        gym_url=GYM_URL, storage_state=STORAGE_STATE, headless=True
    ) as computer:
        computer.wait_for_ready()
        assert computer.is_authenticated(), "expected to bypass /auth/login"


@needs_gym
def test_offsite_navigation_is_refused() -> None:
    """The domain lock is an environment invariant, enforced before Playwright is asked.

    Checked before upstream's https:// prepending, so the message names the URL the model
    actually requested.
    """
    with GymComputer(gym_url=GYM_URL, headless=True) as computer:
        with pytest.raises(ActionRefused) as exc:
            computer.navigate("https://www.google.com")
        assert "refused" in str(exc.value)
        assert computer.refusals, "refusal should be recorded for the artifact"
        # The agent stays where it was rather than being silently teleported.
        assert same_origin(computer.current_state().url, GYM_URL)


@needs_gym
def test_search_action_cannot_leave_the_gym() -> None:
    """`search` normally jumps to a search engine. It stays in the tool set -- the lock
    refuses it -- so the agent gets an observation instead of a missing capability."""
    with GymComputer(gym_url=GYM_URL, headless=True) as computer:
        with pytest.raises(ActionRefused):
            computer.search()


@needs_gym
def test_prohibited_actions_are_enforced() -> None:
    """Empty by default; this proves the knob works when a run does set it."""
    with GymComputer(
        gym_url=GYM_URL, headless=True, prohibited_actions=("scroll_document",)
    ) as computer:
        computer.check_action_allowed("click")  # not prohibited -> no raise
        with pytest.raises(ActionRefused):
            computer.check_action_allowed("scroll_document")


@needs_gym
def test_screenshot_shape_matches_the_model_contract() -> None:
    """PNG bytes at the viewport size the agent's coordinates are denormalized against."""
    with GymComputer(gym_url=GYM_URL, headless=True) as computer:
        state = computer.current_state()
        assert state.screenshot[:8] == b"\x89PNG\r\n\x1a\n"
        width = int.from_bytes(state.screenshot[16:20], "big")
        height = int.from_bytes(state.screenshot[20:24], "big")
        assert (width, height) == tuple(computer.screen_size())
