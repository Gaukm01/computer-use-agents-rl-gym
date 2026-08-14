"""Makes the vendored computer-use-preview clone importable.

The clone lives at agent/computer-use-preview/ and is git-ignored (it has its own history).
It is not a package and has no setup.py, so it has to go on sys.path by hand. Import this
module before importing anything from `agent` or `computers`.
"""

from __future__ import annotations

import pathlib
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR_REPO = PROJECT_ROOT / "agent" / "computer-use-preview"
RUNS_DIR = PROJECT_ROOT / "runs"
TASKS_FILE = PROJECT_ROOT / "resources" / "tasks.json"
STORAGE_STATE = PROJECT_ROOT / "agent" / "storage_state.json"

if not VENDOR_REPO.is_dir():
    raise RuntimeError(
        f"Vendored repo missing at {VENDOR_REPO}.\n"
        "Clone it with:\n"
        "  cd agent && git clone --depth 1 "
        "https://github.com/google-gemini/computer-use-preview.git"
    )

if str(VENDOR_REPO) not in sys.path:
    # MUST be prepended, not appended. The project root is normally first on sys.path and
    # contains an `agent/` directory with no __init__.py, which Python treats as a namespace
    # package (PEP 420). That would shadow the vendored `agent.py` and make
    # `from agent import BrowserAgent` fail with a confusing ImportError. Prepending the
    # clone means the real module wins.
    sys.path.insert(0, str(VENDOR_REPO))
