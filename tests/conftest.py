"""Make the flat ``aegisx_agent`` package importable without an editable install.

The package lives at the repository root (no ``src/`` layout), but pytest only
puts each test file's own directory on ``sys.path`` — not the repo root. This
shim adds the root so the suite runs from a plain ``pytest`` invocation even
when the package is not pip-installed.

It also pins the console width for the whole session: Rich sizes rendered help
and tables from the environment (COLUMNS / TTY width). CI runners have no TTY
and fall back to 80 columns, laptops differ again — substring assertions on
rendered output would then pass or fail depending on where the suite runs.
A generous fixed width makes rendering identical everywhere.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Forced (not setdefault): a developer shell exporting COLUMNS must not
# change what the tests assert on.
os.environ["COLUMNS"] = "200"
