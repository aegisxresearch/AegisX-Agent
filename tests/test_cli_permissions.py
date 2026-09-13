"""CLI surface of the permission gate: the flag and the ``/permissions`` command."""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from aegisx_agent.cli import main as cli
from aegisx_agent.config import AgentConfig, LLMProvider
from aegisx_agent.core import AegisXAgent
from aegisx_agent.security.permissions import PermissionMode


@pytest.fixture()
def runner(tmp_path, monkeypatch):
    """Isolated CLI runner: temp data dir, no saved config, no cached agent."""
    monkeypatch.setenv("AEGISX_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("AEGISX_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(cli, "_agent", None)
    return CliRunner()


@pytest.fixture()
def agent(tmp_path, monkeypatch) -> AegisXAgent:
    """An offline agent pointed at a temp data dir, shared by the slash tests."""
    instance = AegisXAgent(
        AgentConfig(
            llm_provider=LLMProvider.OLLAMA,
            data_dir=str(tmp_path),
            rag_enabled=False,
            web_search_enabled=False,
        ),
        prompter=cli._permission_prompt,
    )
    monkeypatch.setattr(cli, "_agent", instance)
    return instance


def _invoke(runner: CliRunner, *args: str) -> Any:
    return runner.invoke(cli.app, list(args))


def test_chat_exposes_the_permission_mode_flag(runner: CliRunner) -> None:
    result = _invoke(runner, "chat", "--help")

    assert result.exit_code == 0
    assert "--permission-mode" in result.output


def test_the_global_flag_is_also_accepted(runner: CliRunner) -> None:
    """``aegisx --permission-mode ...`` should work like the other global flags."""
    assert "--permission-mode" in _invoke(runner, "--help").output
    assert _invoke(runner, "--permission-mode", "yolo").exit_code != 0


def test_plan_and_schedule_run_expose_the_flag(runner: CliRunner) -> None:
    assert "--permission-mode" in _invoke(runner, "plan", "--help").output
    assert "--permission-mode" in _invoke(runner, "schedule", "run", "--help").output


def test_an_unknown_permission_mode_is_rejected(runner: CliRunner) -> None:
    result = _invoke(runner, "chat", "--permission-mode", "yolo")

    assert result.exit_code != 0
    assert "unknown permission mode" in result.output


def test_the_schedule_run_help_explains_unattended_denial(runner: CliRunner) -> None:
    output = _invoke(runner, "schedule", "run", "--help").output

    assert "nobody can approve" in output


def test_permissions_command_lists_the_policy(agent, capsys) -> None:
    cli._handle_slash_command("/permissions", agent)
    output = capsys.readouterr().out

    assert "Tool Permissions" in output
    assert "ask" in output
    assert "audit.log" in output


def test_permissions_command_shows_gated_tools(agent, capsys) -> None:
    cli._handle_slash_command("/permissions", agent)
    output = capsys.readouterr().out

    assert "execute_code (dangerous)" in output


def test_permissions_mode_switches_and_persists(agent) -> None:
    cli._handle_slash_command("/permissions mode read-only", agent)

    assert agent.permission_gate.mode is PermissionMode.READ_ONLY
    assert agent.config.permission_mode is PermissionMode.READ_ONLY


def test_permissions_mode_rejects_an_unknown_value(agent, capsys) -> None:
    cli._handle_slash_command("/permissions mode nonsense", agent)

    assert agent.permission_gate.mode is PermissionMode.ASK
    assert "Unknown mode" in capsys.readouterr().out


def test_permissions_allow_then_reset(agent) -> None:
    cli._handle_slash_command("/permissions allow execute_code", agent)
    assert agent.permission_gate.allowed_tools == {"execute_code"}

    cli._handle_slash_command("/permissions reset execute_code", agent)
    assert agent.permission_gate.allowed_tools == set()
    assert agent.permission_gate.denied_tools == set()


def test_permissions_deny_blocks_a_safe_tool(agent, capsys) -> None:
    cli._handle_slash_command("/permissions deny calculator", agent)

    assert agent.get_permission_info()["denied"] == ["calculator"]
    assert "deny: calculator" in capsys.readouterr().out


def test_permissions_rejects_an_unknown_tool(agent, capsys) -> None:
    cli._handle_slash_command("/permissions allow no_such_tool", agent)
    output = capsys.readouterr().out

    assert "No such tool" in output
    assert agent.permission_gate.allowed_tools == set()


def test_permissions_usage_hint_without_arguments(agent, capsys) -> None:
    cli._handle_slash_command("/permissions mode", agent)
    assert "Usage" in capsys.readouterr().out


def test_recent_denials_are_visible_in_the_command(agent, capsys) -> None:
    """A denial is the thing an operator most needs to see afterwards."""
    from support import run

    run(agent.tools.execute("file_ops", {"action": "delete", "path": "/tmp/x"}))
    capsys.readouterr()

    cli._handle_slash_command("/permissions", agent)
    output = capsys.readouterr().out

    assert "Recent decisions" in output
    assert "denied" in output
    assert "file_ops" in output
