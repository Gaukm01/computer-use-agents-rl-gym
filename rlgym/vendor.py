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
    # Appended rather than prepended: the clone contains a top-level `agent.py`, and
    # prepending would let it shadow our own `rlgym.agents` package on some import paths.
    sys.path.append(str(VENDOR_REPO))
