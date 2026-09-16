"""Make the flat ``aegisx_agent`` package importable without an editable install.

The package lives at the repository root (no ``src/`` layout), but pytest only
puts each test file's own directory on ``sys.path`` — not the repo root. This
shim adds the root so the suite runs from a plain ``pytest`` invocation even
when the package is not pip-installed.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
