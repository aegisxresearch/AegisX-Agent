"""Coding-mode slash commands: /code, /git, /test."""

from __future__ import annotations

import asyncio

from rich.panel import Panel

from aegisx_agent.cli.app import console


def _handle_code_command(args: str, agent):
    """Handle /code subcommands."""
    parts = args.split(maxsplit=1)
    subcmd = parts[0] if parts else ""
    arg = parts[1] if len(parts) > 1 else "."

    match subcmd:
        case "structure" | "tree":
            result = asyncio.run(
                agent.tools.execute("codebase", {"action": "structure", "path": arg})
            )
            console.print(result.output)
        case "find":
            result = asyncio.run(
                agent.tools.execute("codebase", {"action": "find", "path": ".", "query": arg})
            )
            console.print(result.output)
        case "search":
            result = asyncio.run(
                agent.tools.execute("codebase", {"action": "search", "path": ".", "query": arg})
            )
            console.print(result.output)
        case "read":
            result = asyncio.run(agent.tools.execute("codebase", {"action": "read", "path": arg}))
            console.print(result.output)
        case "deps":
            result = asyncio.run(agent.tools.execute("codebase", {"action": "deps", "path": arg}))
            console.print(result.output)
        case "summary":
            result = asyncio.run(
                agent.tools.execute("codebase", {"action": "summary", "path": arg})
            )
            console.print(result.output)
        case _:
            console.print("[dim]Usage:[/dim]")
            console.print("  /code structure [path]     — Show project tree")
            console.print("  /code find <pattern>       — Find files by name")
            console.print("  /code search <query>       — Search content in files")
            console.print("  /code read <file>          — Read a code file")
            console.print("  /code deps [path]          — Show dependencies")
            console.print("  /code summary [path]       — Project overview")


def _handle_git_command(args: str, agent):
    """Handle /git subcommands."""
    parts = args.split(maxsplit=1)
    subcmd = parts[0] if parts else "status"
    arg = parts[1] if len(parts) > 1 else ""

    match subcmd:
        case "status" | "st":
            result = asyncio.run(agent.tools.execute("git", {"action": "status"}))
            console.print(result.output)
        case "diff" | "di":
            result = asyncio.run(agent.tools.execute("git", {"action": "diff", "args": arg}))
            console.print(result.output)
        case "commit" | "ci":
            if not arg:
                console.print("[dim]Usage: /git commit <message>[/dim]")
            else:
                result = asyncio.run(
                    agent.tools.execute("git", {"action": "commit", "message": arg})
                )
                console.print(result.output)
        case "log" | "lg":
            result = asyncio.run(agent.tools.execute("git", {"action": "log"}))
            console.print(result.output)
        case "branch" | "br":
            result = asyncio.run(agent.tools.execute("git", {"action": "branch", "branch": arg}))
            console.print(result.output)
        case _:
            console.print("[dim]Usage:[/dim]")
            console.print("  /git status       — Show working tree status")
            console.print("  /git diff         — Show changes")
            console.print("  /git commit <msg> — Commit changes")
            console.print("  /git log          — Show commit log")
            console.print("  /git branch       — List branches")


def _handle_test_command(args: str, agent):
    """Handle /test command."""
    path = args.strip() if args.strip() else "."
    console.print(f"[yellow]🧪 Running tests in {path}...[/yellow]")
    result = asyncio.run(agent.tools.execute("run_tests", {"path": path}))
    if result.is_success:
        console.print(Panel(result.output, title="✅ Tests Passed", border_style="green"))
    else:
        console.print(Panel(result.output, title="❌ Tests Failed", border_style="red"))
