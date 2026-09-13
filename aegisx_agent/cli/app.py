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

app = typer.Typer(
    name="aegisx",
    help="🤖 AegisX Agent — Super-powered Agentic AI",
    rich_markup_mode="rich",
)
