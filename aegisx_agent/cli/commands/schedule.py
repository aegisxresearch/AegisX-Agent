"""Scheduled-task slash helpers: /schedule parsing and rendering."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import typer
from rich.table import Table

from aegisx_agent.cli.app import console

if TYPE_CHECKING:
    from aegisx_agent.core import AegisXAgent

SCHEDULE_FLAGS = (
    "--interval",
    "--every",
    "--daily",
    "--weekly",
    "--cron",
    "--persona",
    "--timeout",
)

# ``--every`` is accepted as a friendlier alias for ``--interval``.
_FLAG_ALIASES = {"--every": "--interval"}

SCHEDULE_USAGE = (
    "[dim]Usage:[/dim]\n"
    "  /schedule add <name> <prompt> --interval 30m   — every 30 minutes\n"
    "  /schedule add <name> <prompt> --daily 09:00    — once a day\n"
    "  /schedule add <name> <prompt> --weekly MON:09:00 — once a week\n"
    "  /schedule add <name> <prompt> --cron '*/15 * * * *' — raw cron\n"
    "  /schedule list                                — show tasks\n"
    "  /schedule run                                 — execute everything due now\n"
    "  /schedule logs <task-id>                      — show run history\n"
    "  /schedule cancel <task-id>                    — pause a task now\n"
    "  /schedule resume <task-id>                    — resume from checkpoint\n"
    "  /schedule checkpoint <task-id>                — show latest checkpoint\n"
    "  /schedule remove <task-id>                    — delete a task"
)


def _split_flags(text: str) -> tuple[str, dict[str, str]]:
    """Split ``--flag value`` pairs out of a raw argument string.

    Flags are removed from the body so a prompt containing them is not corrupted.
    """
    tokens = text.split()
    body: list[str] = []
    flags: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in SCHEDULE_FLAGS:
            if index + 1 >= len(tokens):
                raise ValueError(f"{token} requires a value")
            flags[_FLAG_ALIASES.get(token, token)] = tokens[index + 1]
            index += 2
            continue
        body.append(token)
        index += 1
    return " ".join(body), flags


def _flags_to_schedule(flags: dict[str, str]) -> tuple[str, str]:
    """Map parsed flags onto a ``(schedule_type, schedule_value)`` pair."""
    for flag, schedule_type in (
        ("--interval", "interval"),
        ("--daily", "daily"),
        ("--weekly", "weekly"),
        ("--cron", "cron"),
    ):
        if flag in flags:
            return schedule_type, flags[flag]
    raise ValueError(
        "no schedule given: use --interval 30m, --daily 09:00, "
        "--weekly MON:09:00, or --cron '*/15 * * * *'"
    )


def _resolve_schedule(
    every: str | None, daily: str | None, weekly: str | None, cron: str | None
) -> tuple[str, str]:
    """Validate the mutually exclusive schedule options of ``aegisx schedule add``."""
    provided = {
        flag: value
        for flag, value in (
            ("--interval", every),
            ("--daily", daily),
            ("--weekly", weekly),
            ("--cron", cron),
        )
        if value
    }
    if not provided:
        raise typer.BadParameter("provide one of --interval, --daily, --weekly, or --cron")
    if len(provided) > 1:
        raise typer.BadParameter("provide only one schedule flag")
    return _flags_to_schedule(provided)


def _print_schedule_results(results: list[dict[str, Any]]) -> None:
    """Render the outcome of a scheduler run."""
    if not results:
        console.print("[dim]No tasks are due.[/dim]")
        return
    table = Table(title="⏰ Executed Tasks", border_style="yellow")
    table.add_column("Task", style="bold")
    table.add_column("Status")
    table.add_column("Result", max_width=60)
    for entry in results:
        icon = "✅" if entry["status"] == "completed" else "❌"
        table.add_row(entry["name"], f"{icon} {entry['status']}", entry["result"][:200])
    console.print(table)


def _print_schedule_logs(agent: AegisXAgent, task_id: str, limit: int = 10) -> None:
    """Render the run history of one scheduled task."""
    logs = agent.get_scheduled_task_logs(task_id, limit=limit)
    if not logs:
        console.print(f"[dim]No run history for task {task_id}.[/dim]")
        return
    table = Table(title=f"📜 Logs for {task_id}", border_style="cyan")
    table.add_column("Time")
    table.add_column("Status")
    table.add_column("Duration")
    table.add_column("Result", max_width=50)
    for entry in logs:
        table.add_row(
            entry["timestamp"],
            entry["status"],
            f"{entry['duration']:.2f}s",
            entry["result"][:120],
        )
    console.print(table)


def _handle_schedule_command(args: str, agent: AegisXAgent) -> None:
    """Handle /schedule subcommands."""
    parts = args.split(maxsplit=1)
    subcmd = parts[0] if parts else ""
    rest = parts[1] if len(parts) > 1 else ""

    match subcmd:
        case "add":
            try:
                body, flags = _split_flags(rest)
                name, _, prompt = body.partition(" ")
                if not name or not prompt.strip():
                    raise ValueError("usage: /schedule add <name> <prompt> --interval 30m")
                schedule_type, schedule_value = _flags_to_schedule(flags)
                task = agent.add_scheduled_task(
                    name,
                    prompt.strip(),
                    schedule_type,
                    schedule_value,
                    persona=flags.get("--persona", "default"),
                    timeout=int(flags.get("--timeout", 120)),
                )
            except ValueError as exc:
                console.print(f"[error]{exc}[/error]")
                return
            console.print(
                f"[success]✅ Scheduled '{task['name']}' "
                f"({schedule_type}: {schedule_value})[/success]"
            )
            console.print(f"[dim]Next run: {task['next_run'] or 'not scheduled'}[/dim]")

        case "list" | "ls":
            tasks = agent.list_scheduled_tasks()
            if not tasks:
                console.print("[dim]No scheduled tasks.[/dim]")
                return
            table = Table(title="⏰ Scheduled Tasks", border_style="yellow")
            table.add_column("ID", style="bold")
            table.add_column("Name")
            table.add_column("Schedule")
            table.add_column("Next run")
            table.add_column("Last run")
            table.add_column("Enabled")
            for t in tasks:
                table.add_row(
                    t["id"],
                    t["name"],
                    f"{t['schedule_type']}: {t['schedule_value']}",
                    t["next_run"] or "-",
                    t["status"],
                    "✅" if t["enabled"] else "⏸️",
                )
            console.print(table)

        case "run":
            _print_schedule_results(asyncio.run(agent.run_due_scheduled_tasks()))

        case "logs":
            if not rest.strip():
                console.print("[dim]Usage: /schedule logs <task-id>[/dim]")
                return
            _print_schedule_logs(agent, rest.strip())

        case "cancel":
            if not rest.strip():
                console.print("[dim]Usage: /schedule cancel <task-id>[/dim]")
                return
            if agent.request_scheduler_cancel(rest.strip()):
                task_id = rest.strip()
                console.print(f"[warning]⏸️  Cancel requested — task {task_id} paused[/warning]")
            else:
                console.print(f"[error]Task not found: {rest.strip()}[/error]")

        case "resume":
            if not rest.strip():
                console.print("[dim]Usage: /schedule resume <task-id>[/dim]")
                return
            if agent.resume_scheduled_task(rest.strip()):
                console.print(f"[success]▶️  Resumed task {rest.strip()}[/success]")
            else:
                console.print(f"[error]Task not found: {rest.strip()}[/error]")

        case "checkpoint":
            if not rest.strip():
                console.print("[dim]Usage: /schedule checkpoint <task-id>[/dim]")
                return
            checkpoint = agent.get_scheduler_checkpoint(rest.strip())
            if checkpoint is None:
                console.print(f"[error]Task not found: {rest.strip()}[/error]")
                return
            table = Table(title=f"📍 Checkpoint {rest.strip()}", border_style="cyan")
            table.add_column("Field", style="bold")
            table.add_column("Value", max_width=70)
            for key, value in checkpoint.items():
                table.add_row(key, str(value)[:120])
            console.print(table)

        case "remove" | "rm":
            if not rest.strip():
                console.print("[dim]Usage: /schedule remove <task-id>[/dim]")
                return
            if agent.remove_scheduled_task(rest.strip()):
                console.print(f"[success]✅ Removed task {rest.strip()}[/success]")
            else:
                console.print(f"[error]Task not found: {rest.strip()}[/error]")

        case _:
            console.print(SCHEDULE_USAGE)


schedule_app = typer.Typer(help="⏰ Manage and run scheduled tasks")
