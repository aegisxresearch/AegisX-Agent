"""CLI scheduler wiring: add, list, remove, logs, and really running due tasks."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from typer.testing import CliRunner

from aegisx_agent import cli
from aegisx_agent.core import AegisXAgent


@pytest.fixture()
def runner(tmp_path, monkeypatch):
    """Isolated CLI runner: temp data dir, no saved config, no cached agent."""
    monkeypatch.setenv("AEGISX_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("AEGISX_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(cli, "_agent", None)
    return CliRunner()


def _invoke(runner: CliRunner, *args: str) -> Any:
    return runner.invoke(cli.app, list(args))


def _make_due(agent: AegisXAgent, task_id: str) -> None:
    task = agent.scheduler.get_task(task_id)
    assert task is not None
    task.next_run = (datetime.now() - timedelta(minutes=1)).isoformat()


def test_schedule_command_is_registered(runner: CliRunner) -> None:
    result = _invoke(runner, "--help")

    assert result.exit_code == 0
    assert "schedule" in result.output


def test_schedule_add_then_list(runner: CliRunner) -> None:
    added = _invoke(runner, "schedule", "add", "greet", "say hi", "--interval", "30m")
    assert added.exit_code == 0, added.output
    assert "greet" in added.output

    listing = _invoke(runner, "schedule", "list")
    assert listing.exit_code == 0, listing.output
    assert "greet" in listing.output
    assert "interval: 30m" in listing.output


def test_schedule_add_requires_exactly_one_schedule_flag(runner: CliRunner) -> None:
    missing = _invoke(runner, "schedule", "add", "greet", "say hi")
    assert missing.exit_code != 0

    conflicting = _invoke(
        runner,
        "schedule",
        "add",
        "greet",
        "say hi",
        "--interval",
        "30m",
        "--daily",
        "09:00",
    )
    assert conflicting.exit_code != 0


def test_schedule_remove_missing_task_fails(runner: CliRunner) -> None:
    result = _invoke(runner, "schedule", "remove", "does-not-exist")

    assert result.exit_code == 1
    assert "not found" in result.output.lower()


def test_schedule_run_once_reports_nothing_due(runner: CliRunner) -> None:
    result = _invoke(runner, "schedule", "run", "--once")

    assert result.exit_code == 0, result.output
    assert "No tasks are due" in result.output


def test_schedule_run_once_executes_due_task(runner: CliRunner, monkeypatch) -> None:
    assert (
        _invoke(runner, "schedule", "add", "greet", "say hi", "--interval", "30m").exit_code == 0
    )

    agent = cli._get_agent()
    task_id = agent.list_scheduled_tasks()[0]["id"]
    _make_due(agent, task_id)

    seen: list[str] = []

    async def fake_chat(self: AegisXAgent, prompt: str) -> str:
        seen.append(prompt)
        return "hello from the scheduler"

    monkeypatch.setattr(AegisXAgent, "chat", fake_chat)

    result = _invoke(runner, "schedule", "run", "--once")

    assert result.exit_code == 0, result.output
    assert "hello from the scheduler" in result.output
    assert seen == ["say hi"]

    # the run is persisted, so a fresh agent sees the updated counters
    monkeypatch.setattr(cli, "_agent", None)
    stored = cli._get_agent().list_scheduled_tasks()[0]
    assert stored["run_count"] == 1
    assert stored["status"] == "completed"


def test_schedule_logs_show_history(runner: CliRunner, monkeypatch) -> None:
    _invoke(runner, "schedule", "add", "greet", "say hi", "--interval", "30m")
    agent = cli._get_agent()
    task_id = agent.list_scheduled_tasks()[0]["id"]
    _make_due(agent, task_id)

    async def fake_chat(self: AegisXAgent, prompt: str) -> str:
        return "logged run"

    monkeypatch.setattr(AegisXAgent, "chat", fake_chat)
    _invoke(runner, "schedule", "run", "--once")

    logs = _invoke(runner, "schedule", "logs", task_id)

    assert logs.exit_code == 0, logs.output
    assert "completed" in logs.output


def test_slash_schedule_parses_flags_without_corrupting_prompt() -> None:
    body, flags = cli._split_flags("nightly run the report --interval 2h")

    assert body == "nightly run the report"
    assert flags == {"--interval": "2h"}
    assert cli._flags_to_schedule(flags) == ("interval", "2h")


def test_slash_schedule_flag_errors() -> None:
    with pytest.raises(ValueError):
        cli._split_flags("task --interval")
    with pytest.raises(ValueError):
        cli._flags_to_schedule({})


def test_resolve_schedule_rejects_conflicting_flags() -> None:
    import typer

    assert cli._resolve_schedule("30m", None, None, None) == ("interval", "30m")
    with pytest.raises(typer.BadParameter):
        cli._resolve_schedule(None, None, None, None)
    with pytest.raises(typer.BadParameter):
        cli._resolve_schedule("30m", "09:00", None, None)
