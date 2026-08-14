"""RL gym: run one attempt against the Metabase environment and grade it.

Importing this package puts the vendored computer-use-preview clone on sys.path, so no
module below needs a load-bearing import ordering. Previously `computer.py` and
`gemini_cu.py` each had to import `vendor` *before* `from computers import ...`, which any
import sorter (isort, ruff) would have silently reordered and broken.
"""

from . import vendor as vendor  # noqa: F401  -- side effect: sets up sys.path
