"""SchedulerDaemon: due execution, catch-up cap, and cooperative shutdown."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from support import run

from aegisx_agent.scheduler.daemon import (
    DEFAULT_POLL_SECONDS,
    SchedulerDaemon,
    format_status,
)
from aegisx_agent.scheduler.engine import Scheduler


class FakeAgent:
    """Minimal stand-in for AegisXAgent so runs never touch a network."""

    def __init__(self, reply: str = "daemon-ok", delay: float = 0.0) -> None:
        self.reply = reply
        self.delay = delay
        self.prompts: list[str] = []
        self.config = SimpleNamespace(persona="default")

    async def chat(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.reply


def _daemon(tmp_path: Any, agent: FakeAgent | None = None, **kwargs: Any) -> SchedulerDaemon:
    scheduler = Scheduler(
        data_dir=tmp_path, agent_factory=lambda: agent or FakeAgent()
    )
    return SchedulerDaemon(scheduler=scheduler, **kwargs)


def _make_due(scheduler: Scheduler, task_id: str) -> None:
    task = scheduler.get_task(task_id)
    assert task is not None
    task.next_run = (datetime.now() - timedelta(minutes=1)).isoformat()


def test_default_poll_is_thirty_seconds() -> None:
    assert DEFAULT_POLL_SECONDS == 30.0


def test_daemon_executes_due_tasks_and_schedules_next(tmp_path) -> None:
    daemon = _daemon(tmp_path)
    task = daemon.scheduler.add_task("tick", "say hi", "interval", "30m")
    _make_due(daemon.scheduler, task.id)

    run(daemon._cycle())

    stored = daemon.scheduler.get_task(task.id)
    assert stored is not None
    assert stored.run_count == 1
    assert stored.status.value == "completed"
    # next run lands ~30m in the future, not in the past
    assert datetime.fromisoformat(stored.next_run) > datetime.now() + timedelta(minutes=29)
    assert daemon._stats["runs"] == 1


def test_catch_up_limit_caps_a_single_cycle(tmp_path) -> None:
    daemon = _daemon(tmp_path, catch_up_limit=2)
    ids = [
        daemon.scheduler.add_task(f"t{i}", "work", "once", "").id for i in range(5)
    ]
    for task_id in ids:
        _make_due(daemon.scheduler, task_id)

    run(daemon._cycle())

    assert daemon._stats["runs"] == 2
    assert daemon._stats["catch_up_skips"] == 3


def test_stop_event_ends_run_forever_quickly(tmp_path) -> None:
    daemon = _daemon(tmp_path, poll_seconds=5)

    async def _scenario() -> float:
        loop_task = asyncio.create_task(daemon.run_forever())
        await asyncio.sleep(0.3)
        started = datetime.now()
        daemon.request_stop()
        await asyncio.wait_for(loop_task, timeout=5)
        return (datetime.now() - started).total_seconds()

    elapsed = run(_scenario())
    assert elapsed < 3  # far below the 5s poll: the stop event is honored


def test_run_for_executes_due_tasks_then_exits(tmp_path) -> None:
    daemon = _daemon(tmp_path, poll_seconds=0.2)
    task = daemon.scheduler.add_task("quick", "work", "interval", "30m")
    _make_due(daemon.scheduler, task.id)

    run(daemon.run_for(0.5))

    stored = daemon.scheduler.get_task(task.id)
    assert stored is not None and stored.run_count == 1
    assert daemon._stats["runs"] == 1


def test_status_snapshot_reflects_state(tmp_path) -> None:
    daemon = _daemon(tmp_path)
    daemon.scheduler.add_task("job", "work", "daily", "23:59")

    snapshot = daemon.status()

    assert snapshot["tasks"] == 1
    assert snapshot["enabled"] == 1
    assert snapshot["poll_seconds"] == DEFAULT_POLL_SECONDS
    assert snapshot["stop_requested"] is False
    assert snapshot["next_run"]
    formatted = format_status(snapshot)
    assert "poll=30s" in formatted and "tasks=1" in formatted


def test_daemon_recovers_runs_interrupted_by_restart(tmp_path) -> None:
    """A task left RUNNING by a killed daemon is reset on the next start."""
    scheduler = Scheduler(data_dir=tmp_path, agent_factory=lambda: FakeAgent())
    task = scheduler.add_task("stuck", "work", "interval", "30m")
    task.status = task.status.__class__("running") if False else __import__(
        "aegisx_agent.scheduler.task", fromlist=["TaskStatus"]
    ).TaskStatus.RUNNING
    scheduler._save_task(task)  # noqa: SLF001 - simulating a crash mid-run

    revived = Scheduler(data_dir=tmp_path, agent_factory=lambda: FakeAgent())

    stored = revived.get_task(task.id)
    assert stored is not None
    assert stored.status.value == "pending"
    assert stored.checkpoint.get("state") == "resumed_after_restart"


def test_foreground_handler_runs_and_stops(tmp_path, capsys) -> None:
    """The /daemon run handler starts, executes a due task, and returns."""
    from aegisx_agent.cli.commands.daemon import _run_daemon_foreground

    scheduler = Scheduler(data_dir=tmp_path, agent_factory=lambda: FakeAgent())
    task = scheduler.add_task("fg", "work", "interval", "30m")
    _make_due(scheduler, task.id)
    agent = SimpleNamespace(scheduler=scheduler)

    # Patch run_forever to a bounded variant so the test cannot hang.

    original_run_forever = SchedulerDaemon.run_forever

    async def bounded(self: SchedulerDaemon) -> None:
        await self._cycle()
        self.request_stop()

    SchedulerDaemon.run_forever = bounded  # type: ignore[method-assign]
    try:
        _run_daemon_foreground(agent, 0.2)  # type: ignore[arg-type]
    finally:
        SchedulerDaemon.run_forever = original_run_forever  # type: ignore[method-assign]

    out = capsys.readouterr().out
    assert "Daemon started" in out
    assert "Daemon stopped" in out
    stored = scheduler.get_task(task.id)
    assert stored is not None and stored.run_count == 1


def test_daemon_add_slash_handler_parses_flags(tmp_path, capsys) -> None:
    from aegisx_agent.cli.commands.daemon import _handle_daemon_command

    scheduler = Scheduler(data_dir=tmp_path, agent_factory=lambda: FakeAgent())
    agent = SimpleNamespace(scheduler=scheduler)

    _handle_daemon_command(
        "add nightly summarize inbox --daily 09:00 --persona coder", agent
    )
    _handle_daemon_command("add broken --bogus x", agent)
    out = capsys.readouterr().out

    assert "scheduled" in out
    assert "--daily" in out  # unknown flag surfaces in the usage/error output
    stored = scheduler.list_tasks()[0]
    assert stored.name == "nightly"
    assert stored.schedule_type.value == "daily"
    assert stored.schedule_value == "09:00"
    assert stored.persona == "coder"


def test_daemon_usage_and_unknown_subcommand(tmp_path, capsys) -> None:
    from aegisx_agent.cli.commands.daemon import _handle_daemon_command

    agent = SimpleNamespace(scheduler=Scheduler(tmp_path))
    _handle_daemon_command("", agent)
    _handle_daemon_command("bogus", agent)
    out = capsys.readouterr().out

    assert out.count("/daemon status") >= 2
    assert "Unknown /daemon subcommand" in out
