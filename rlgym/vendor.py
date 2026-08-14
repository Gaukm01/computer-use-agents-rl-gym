"""Project paths, and putting the vendored computer-use-preview clone on sys.path.

The clone lives at agent/computer-use-preview/ and is git-ignored (it has its own history).
It is not a package and has no setup.py, so it has to go on sys.path by hand.

That happens in `rlgym/__init__.py`, which imports this module -- so importing *any*
`rlgym.*` module sets the path up first. Nothing here is order-dependent: modules that need
the vendored code can just `from computers import ...` at the top of the file like any other
import.

The path insert is deliberately silent when the clone is missing, so `rlgym.schemas`,
`rlgym.grader` and `rlgym.tasks` -- none of which touch the vendored code -- stay importable
on a fresh checkout. Call `require()` from an entry point that genuinely needs the clone to
get a helpful message instead of a bare ImportError.
"""

from __future__ import annotations

import pathlib
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR_REPO = PROJECT_ROOT / "agent" / "computer-use-preview"
RUNS_DIR = PROJECT_ROOT / "runs"
TASKS_FILE = PROJECT_ROOT / "resources" / "tasks.json"
STORAGE_STATE = PROJECT_ROOT / "agent" / "storage_state.json"
# Task files uploaded through the UI. Kept out of resources/ so the pristine tasks.json
# is never overwritten by an upload.
UPLOADS_DIR = PROJECT_ROOT / "uploads"

CLONE_HINT = (
    f"Vendored repo missing at {VENDOR_REPO}.\n"
    "Clone it with:\n"
    "  cd agent && git clone --depth 1 "
    "https://github.com/google-gemini/computer-use-preview.git"
)


def _install_path() -> None:
    """Prepend the clone to sys.path.

    MUST be prepended, not appended. The project root is normally first on sys.path and
    contains an `agent/` directory with no __init__.py, which Python treats as a namespace
    package (PEP 420). That would shadow the vendored `agent.py` and make
    `from agent import BrowserAgent` fail with a confusing ImportError.
    """
    if not VENDOR_REPO.is_dir():
        return
    path = str(VENDOR_REPO)
    if path not in sys.path:
        sys.path.insert(0, path)


def require() -> None:
    """Fail fast with instructions when the clone is needed but absent."""
    if not VENDOR_REPO.is_dir():
        raise RuntimeError(CLONE_HINT)


_install_path()
