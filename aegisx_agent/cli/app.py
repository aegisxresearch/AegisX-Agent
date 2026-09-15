"""Shared Typer application object, console, and theme for the CLI package."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.theme import Theme

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


def print_delegation_progress(event: str) -> None:
    """Render one line of delegation telemetry for a non-streaming turn.

    ``aegisx chat`` streams these inline; where there is no stream to carry
    them, they are printed dim so the answer still reads as the main event.
    How many arrive is decided by ``AEGISX_SUBAGENT_PROGRESS``, not here.
    """
    console.print(f"[dim]{event}[/dim]")


app = typer.Typer(
    name="aegisx",
    help="🤖 AegisX Agent — Super-powered Agentic AI",
    rich_markup_mode="rich",
)
