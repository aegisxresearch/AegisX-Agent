"""Daemon CLI commands: ``aegisx daemon run`` and the /daemon slash handler."""

from __future__ import annotations

import asyncio
import shlex
from typing import TYPE_CHECKING, Any

import typer
from rich.table import Table

from aegisx_agent.cli.app import console
from aegisx_agent.cli.commands.schedule import _flags_to_schedule, _split_flags
from aegisx_agent.scheduler.daemon import (
    DEFAULT_POLL_SECONDS,
    SchedulerDaemon,
    format_status,
)

if TYPE_CHECKING:
    from aegisx_agent.core import AegisXAgent

DAEMON_USAGE = (
    "[dim]Usage:[/dim]\n"
    "  /daemon status                — show scheduler state\n"
    "  /daemon add <name> <prompt> [flags] — add a task (same flags as /schedule add)\n"
    "  /daemon run [--interval 30]   — run the daemon in the foreground (Ctrl-C to stop)\n"
    "  /daemon list                  — list scheduled tasks"
)


def _build_daemon(agent: AegisXAgent, interval: float) -> SchedulerDaemon:
    """Build a daemon on the agent's scheduler with its agent factory."""
    return SchedulerDaemon(
        scheduler=agent.scheduler,
        poll_seconds=interval,
        catch_up_limit=5,
    )


def _get_cli_agent() -> Any:
    """Lazy import of the CLI agent accessor (avoids a circular import with main)."""
    from aegisx_agent.cli.main import _get_agent, _get_config

    return _get_agent(_get_config())


def _run_daemon_foreground(agent: AegisXAgent, interval: float) -> None:
    """Run the daemon in the foreground until Ctrl-C."""
    daemon = _build_daemon(agent, interval)
    console.print(
        f"[success]🚀 Daemon started — poll every {interval:g}s, "
        f"{len(agent.scheduler.list_tasks())} task(s) on file.[/success]"
    )
    console.print("[dim]Ctrl-C to stop; in-flight runs finish first.[/dim]")
    try:
        asyncio.run(daemon.run_forever())
    except KeyboardInterrupt:
        daemon.request_stop()
    finally:
        console.print(f"[dim]Daemon stopped. {format_status(daemon.status())}[/dim]")


def _daemon_add(agent: AegisXAgent, args: str) -> None:
    """Add a daemon task using the /schedule add flag syntax."""
    parts = args.split(maxsplit=1)
    if not parts:
        console.print("[error]Usage: /daemon add <name> <prompt> [flags][/error]")
        console.print(DAEMON_USAGE)
        return
    name = parts[0]
    try:
        # _split_flags returns (body_without_flags, flags) and raises on a
        # dangling --flag, so misconfiguration fails loudly here.
        prompt, flags = _split_flags(parts[1] if len(parts) > 1 else "")
        schedule_type, schedule_value = _flags_to_schedule(flags)
    except ValueError as exc:
        console.print(f"[error]{exc}[/error]")
        console.print(DAEMON_USAGE)
        return
    if not prompt:
        console.print("[error]A prompt is required after the task name[/error]")
        console.print(DAEMON_USAGE)
        return
    task = agent.scheduler.add_task(
        name=name,
        prompt=prompt,
        schedule_type=schedule_type,
        schedule_value=schedule_value,
        timeout=int(flags.get("--timeout", "120")),
        persona=flags.get("--persona", "default"),
    )
    console.print(
        f"[success]⏰ Task '{task.id[:8]}' '{task.name}' scheduled "
        f"({schedule_type}: {schedule_value}), next run {task.next_run or 'on add'}[/success]"
    )


def _print_daemon_status(agent: AegisXAgent) -> None:
    """Scheduler state table for the current data dir."""
    tasks = agent.scheduler.list_tasks()
    if not tasks:
        console.print(
            "[dim]No scheduled tasks yet. Add one with "
            "/daemon add <name> <prompt> --interval 30m[/dim]"
        )
        return
    table = Table(title="⏰ Scheduled Tasks", border_style="cyan")
    table.add_column("ID", style="dim")
    table.add_column("Name", style="bold")
    table.add_column("Schedule")
    table.add_column("Status")
    table.add_column("Next run", style="dim")
    for task in tasks:
        schedule = f"{task.schedule_type.value}: {task.schedule_value}"
        state = task.status.value if task.enabled else "disabled"
        table.add_row(task.id[:8], task.name, schedule, state, task.next_run or "-")
    console.print(table)


def _handle_daemon_command(args: str, agent: AegisXAgent) -> None:
    """Dispatch ``/daemon <subcommand>``."""
    parts = args.strip().split(maxsplit=1)
    subcommand = parts[0].lower() if parts else ""
    rest = parts[1] if len(parts) > 1 else ""

    if subcommand in ("", "help"):
        console.print(DAEMON_USAGE)
        return
    if subcommand == "status" or subcommand == "list":
        _print_daemon_status(agent)
        return
    if subcommand == "add":
        _daemon_add(agent, rest)
        return
    if subcommand == "run":
        try:
            _body, flags = _split_flags(rest)
            interval = float(flags.get("--interval", str(DEFAULT_POLL_SECONDS)))
        except ValueError as exc:
            console.print(f"[error]{exc}[/error]")
            return
        _run_daemon_foreground(agent, interval)
        return
    console.print(f"[error]Unknown /daemon subcommand: {subcommand}[/error]")
    console.print(DAEMON_USAGE)


def register(app: Any) -> None:  # pragma: no cover - thin Typer wiring
    """Register the ``aegisx daemon`` sub-app on the root Typer app."""
    daemon_app = typer.Typer(help="⏰ Run the persistent scheduler daemon")

    @daemon_app.command("run")
    def daemon_run(
        interval: float = typer.Option(
            DEFAULT_POLL_SECONDS, "--interval", help="Seconds between polls"
        ),
    ) -> None:
        """Run the scheduler daemon in the foreground until Ctrl-C."""
        _run_daemon_foreground(_get_cli_agent(), interval)

    @daemon_app.command("status")
    def daemon_status() -> None:
        """Show scheduled tasks and daemon state."""
        _print_daemon_status(_get_cli_agent())

    @daemon_app.command("add")
    def daemon_add(
        name: str = typer.Argument(help="Task name"),
        prompt: str = typer.Argument(help="Prompt the agent executes on each fire"),
        interval: str | None = typer.Option(
            None, "--interval", "--every", help="e.g. 30m, 1h"
        ),
        daily: str | None = typer.Option(None, "--daily", help="HH:MM local time"),
        cron: str | None = typer.Option(None, "--cron", help='5-field cron, e.g. "0 9 * * 1-5"'),
    ) -> None:
        """Add a persistent task (same schedules as `aegisx schedule add`)."""
        flags = ""
        if interval is not None:
            flags += f" --interval {interval}"
        if daily is not None:
            flags += f" --daily {daily}"
        if cron is not None:
            flags += f" --cron {cron}"
        if not flags:
            console.print("[error]One of --interval, --daily, or --cron is required[/error]")
            raise typer.Exit(code=2)
        _daemon_add(
            _get_cli_agent(),
            f"{shlex.quote(name)} {shlex.quote(prompt)}{flags}",
        )

    @daemon_app.command("remove")
    def daemon_remove(
        task_id: str = typer.Argument(help="Task id (first 8 chars are enough)"),
    ) -> None:
        """Remove a scheduled task."""
        agent = _get_cli_agent()
        removed = agent.scheduler.remove_task(task_id)
        if removed:
            console.print(f"[success]🗑 Removed task {task_id}[/success]")
        else:
            console.print(f"[error]No task matching '{task_id}'[/error]")
            raise typer.Exit(code=1)

    app.add_typer(daemon_app, name="daemon")


__all__ = [
    "DAEMON_USAGE",
    "_build_daemon",
    "_handle_daemon_command",
    "_print_daemon_status",
    "_run_daemon_foreground",
    "register",
]
