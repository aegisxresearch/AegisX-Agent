"""commands/schedule.py: the /schedule slash handler and its renderers.

Runs against a stub agent so every branch — success, error, usage — is hit
deterministically without a database or LLM.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import typer

from aegisx_agent.cli.commands import schedule as sched


class StubAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.tasks: list[dict[str, Any]] = []
        self.logs: list[dict[str, Any]] = []
        self.due_results: list[dict[str, Any]] = []

    def add_scheduled_task(self, name, prompt, schedule_type, schedule_value,
                           persona="default", timeout=120, **kwargs):
        self.calls.append(("add", name, prompt, schedule_type, schedule_value, persona, timeout))
        return {"name": name, "id": "t1", "next_run": "2026-09-14T09:00:00"}

    def list_scheduled_tasks(self):
        return self.tasks

    def run_due_scheduled_tasks(self):
        return asyncio.sleep(0, result=self.due_results) and self.due_results

    async def run_due_async(self):
        return self.due_results

    def get_scheduled_task_logs(self, task_id: str, limit: int = 10):
        return self.logs

    def request_scheduler_cancel(self, task_id: str) -> bool:
        self.calls.append(("cancel", task_id))
        return task_id == "t1"

    def resume_scheduled_task(self, task_id: str) -> bool:
        self.calls.append(("resume", task_id))
        return task_id == "t1"

    def get_scheduler_checkpoint(self, task_id: str):
        self.calls.append(("checkpoint", task_id))
        if task_id == "t1":
            return {"state": "paused", "retry_count": 1}
        return None

    def remove_scheduled_task(self, task_id: str) -> bool:
        self.calls.append(("remove", task_id))
        return task_id == "t1"


@pytest.fixture()
def agent(capsys: pytest.CaptureFixture[str]) -> StubAgent:
    return StubAgent()


def out(capsys: pytest.CaptureFixture[str]) -> str:
    return capsys.readouterr().out


# === pure helpers ===


def test_flags_to_schedule_prefers_the_first_flag_present() -> None:
    assert sched._flags_to_schedule({"--cron": "*/5 * * * *"}) == ("cron", "*/5 * * * *")
    assert sched._flags_to_schedule({"--weekly": "MON:09:00"}) == ("weekly", "MON:09:00")


def test_resolve_schedule_maps_each_flag() -> None:
    assert sched._resolve_schedule("30m", None, None, None) == ("interval", "30m")
    assert sched._resolve_schedule(None, "09:00", None, None) == ("daily", "09:00")
    assert sched._resolve_schedule(None, None, "MON:09:00", None) == ("weekly", "MON:09:00")
    assert sched._resolve_schedule(None, None, None, "*/15 * * * *") == ("cron", "*/15 * * * *")

    with pytest.raises(typer.BadParameter):
        sched._resolve_schedule(None, None, None, None)
    with pytest.raises(typer.BadParameter):
        sched._resolve_schedule("30m", "09:00", None, None)


# === /schedule add ===


def test_schedule_add_parses_flags_and_calls_the_agent(agent, capsys) -> None:
    sched._handle_schedule_command("add nightly run report --interval 2h --persona coder", agent)

    assert agent.calls == [("add", "nightly", "run report", "interval", "2h", "coder", 120)]
    captured = out(capsys)
    assert "Scheduled 'nightly'" in captured
    assert "Next run:" in captured


def test_schedule_add_timeout_flag_reaches_the_agent(agent, capsys) -> None:
    sched._handle_schedule_command("add quick hi --interval 5m --timeout 30", agent)

    assert agent.calls[0][6] == 30


def test_schedule_add_error_paths(agent, capsys) -> None:
    sched._handle_schedule_command("add only-name", agent)
    assert "usage:" in out(capsys)

    sched._handle_schedule_command("add name prompt", agent)  # no schedule flag
    assert "no schedule given" in out(capsys)

    sched._handle_schedule_command("add name prompt --interval", agent)  # missing value
    assert "requires a value" in out(capsys)


# === /schedule list ===


def test_schedule_list_renders_tasks_or_empty_state(agent, capsys) -> None:
    sched._handle_schedule_command("list", agent)
    assert "No scheduled tasks" in out(capsys)

    agent.tasks = [{
        "id": "t1", "name": "nightly", "schedule_type": "interval",
        "schedule_value": "2h", "next_run": None, "run_count": 3,
        "status": "completed", "enabled": True,
    }]
    sched._handle_schedule_command("ls", agent)
    captured = out(capsys)
    assert "nightly" in captured and "interval: 2h" in captured


# === /schedule run ===


def test_schedule_run_renders_due_results(agent, capsys, monkeypatch) -> None:
    async def fake_run():
        return [
            {"name": "nightly", "status": "completed", "result": "all good"},
            {"name": "broken", "status": "failed", "result": "boom"},
        ]

    monkeypatch.setattr(sched.asyncio, "run", lambda coro: fake_run_result)
    fake_run_result = [
        {"name": "nightly", "status": "completed", "result": "all good"},
        {"name": "broken", "status": "failed", "result": "boom"},
    ]

    sched._handle_schedule_command("run", agent)
    captured = out(capsys)
    assert "✅ completed" in captured
    assert "❌ failed" in captured


def test_print_schedule_results_empty(agent, capsys) -> None:
    sched._print_schedule_results([])
    assert "No tasks are due" in out(capsys)


# === /schedule logs ===


def test_schedule_logs_renders_history_or_usage(agent, capsys) -> None:
    sched._handle_schedule_command("logs", agent)
    assert "Usage:" in out(capsys)

    agent.logs = [{
        "timestamp": "2026-09-14T09:00:00", "status": "completed",
        "duration": 1.234, "result": "done work",
    }]
    sched._handle_schedule_command("logs t1", agent)
    captured = out(capsys)
    assert "1.23s" in captured and "done work" in captured


def test_print_schedule_logs_empty(agent, capsys) -> None:
    agent = StubAgent()
    sched._print_schedule_logs(agent, "t9")
    assert "No run history" in out(capsys)


# === /schedule cancel/resume/checkpoint/remove ===


def test_schedule_lifecycle_commands(agent, capsys) -> None:
    sched._handle_schedule_command("cancel t1", agent)
    assert "Cancel requested" in out(capsys)

    sched._handle_schedule_command("cancel t9", agent)
    assert "Task not found" in out(capsys)

    sched._handle_schedule_command("resume t1", agent)
    assert "Resumed task" in out(capsys)

    sched._handle_schedule_command("resume t9", agent)
    assert "Task not found" in out(capsys)

    sched._handle_schedule_command("checkpoint t1", agent)
    captured = out(capsys)
    assert "paused" in captured and "retry_count" in captured

    sched._handle_schedule_command("checkpoint t9", agent)
    assert "Task not found" in out(capsys)

    sched._handle_schedule_command("remove t1", agent)
    assert "Removed task" in out(capsys)

    sched._handle_schedule_command("rm t9", agent)
    assert "Task not found" in out(capsys)


def test_schedule_lifecycle_commands_require_an_id(agent, capsys) -> None:
    for subcmd in ("cancel", "resume", "checkpoint", "remove"):
        sched._handle_schedule_command(subcmd, agent)
        assert "Usage:" in out(capsys), subcmd


# === usage ===


def test_schedule_unknown_subcommand_shows_usage(agent, capsys) -> None:
    sched._handle_schedule_command("teleport", agent)
    captured = out(capsys)
    assert "Usage:" in captured
    assert "--cron" in captured
