"""Shared Typer application object, console, and theme for the CLI package."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.theme import Theme
from rich.traceback import install as _install_rich_traceback

# Readable crash reports for anything that escapes the CLI's own handling:
# locals collapsed, frames indented, instead of one raw wall of Python.
_install_rich_traceback(console=None, show_locals=False, word_wrap=True, extra_lines=1)

THEME = Theme({
    "info": "cyan",
    "success": "green bold",
    "warning": "yellow",
    "error": "bold red",
    "user": "bold blue",
    "agent": "bold green",
    "tool": "bold yellow",
    "cmd": "bold magenta",
    "dim": "dim",
})

console = Console(theme=THEME)

#: Progress and diagnostics. Kept on stderr so a command's machine-readable
#: output can be piped without narration mixed into it — ``aegisx cyber scan
#: --json > findings.json`` has to produce a file a JSON parser can read.
#: The theme is shared, so a dim progress line looks the same on either stream.
err_console = Console(theme=THEME, stderr=True)


def print_delegation_progress(event: str) -> None:
    """Render one line of delegation telemetry for a non-streaming turn.

    ``aegisx chat`` streams these inline; where there is no stream to carry
    them, they are printed dim so the answer still reads as the main event.
    How many arrive is decided by ``AEGISX_SUBAGENT_PROGRESS``, not here.
    """
    console.print(f"[dim]{event}[/dim]")


app = typer.Typer(
    name="aegisx",
    help="🛡️ AegisX Agent — autonomous pentest and bug-hunting agent",
    rich_markup_mode="rich",
)
