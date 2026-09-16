"""Make the flat ``aegisx_agent`` package importable without an editable install.

The package lives at the repository root (no ``src/`` layout), but pytest only
puts each test file's own directory on ``sys.path`` — not the repo root. This
shim adds the root so the suite runs from a plain ``pytest`` invocation even
when the package is not pip-installed.

It also pins the console width and color mode for the whole session. Rich
sizes and styles rendered help and tables from the environment (COLUMNS /
TTY width / FORCE_COLOR), and Typer additionally forces terminal styling
whenever GITHUB_ACTIONS, FORCE_COLOR or PY_COLORS is set — GitHub runners
always export the first, so captured help text comes back full of ANSI
escapes that split flags like ``--permission-mode`` into per-segment chunks
and break substring assertions. ``_TYPER_FORCE_DISABLE_TERMINAL`` (Typer's
official escape hatch) and ``TTY_COMPATIBLE=0`` (checked by Rich before
FORCE_COLOR) force plain, deterministic rendering everywhere; a generous
fixed width does the same for layout.
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
# Same reasoning for color: a runner image exporting FORCE_COLOR (or CI
# exporting GITHUB_ACTIONS) must not turn captured output into styled
# segments. TTY_COMPATIBLE tames Rich consoles the app creates itself;
# _TYPER_FORCE_DISABLE_TERMINAL tames the console Typer builds for --help,
# which sets force_terminal=True when GITHUB_ACTIONS/FORCE_COLOR/PY_COLORS.
os.environ["TTY_COMPATIBLE"] = "0"
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"
