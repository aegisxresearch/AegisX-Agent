"""CLI surface for /usage and /audit plus agent-side tracker wiring."""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from aegisx_agent.cli import main as cli
from aegisx_agent.cli.commands.observability import (
    _handle_audit_command,
    _handle_usage_command,
    _print_usage_summary,
)
from aegisx_agent.config import AgentConfig, LLMProvider
from aegisx_agent.core import AegisXAgent
from aegisx_agent.observability.usage import USAGE_FILE


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


def _agent(tmp_path: Any, **overrides: Any) -> AegisXAgent:
    config = AgentConfig(
        llm_provider=LLMProvider.CUSTOM,
        custom_base_url="http://127.0.0.1:1",  # never reached
        custom_api_key="test-key",
        custom_model="fake-model",
        data_dir=str(tmp_path),
        rag_enabled=False,
        **overrides,
    )
    return AegisXAgent(config)


def _seed_usage(agent: AegisXAgent, rows: list[dict[str, Any]]) -> None:
    path = agent.config.data_path / USAGE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# Agent wiring
# --------------------------------------------------------------------------- #


def test_agent_wraps_llm_with_tracker(tmp_path) -> None:
    agent = _agent(tmp_path)

    assert agent.usage is agent.llm
    assert agent.usage.run_id == agent.session_id
    assert agent.llm.model  # transparent: model still reachable


# --------------------------------------------------------------------------- #
# /usage slash handler
# --------------------------------------------------------------------------- #


def test_usage_handler_prints_totals(tmp_path, capsys) -> None:
    agent = _agent(tmp_path)
    _seed_usage(
        agent,
        [
            {"run_id": "r1", "model": "m1", "calls": 2, "input_tokens": 100,
             "output_tokens": 50, "total_tokens": 150, "timestamp": "2026-09-14T10:00:00"},
            {"run_id": "r2", "model": "m2", "calls": 1, "input_tokens": 10,
             "output_tokens": 5, "total_tokens": 15, "timestamp": "2026-09-14T11:00:00"},
        ],
    )

    _handle_usage_command("", agent)
    out = capsys.readouterr().out

    assert "Total tokens" in out
    assert "165" in out  # 150 + 15
    assert "3" in out  # calls


def test_usage_handler_respects_period_and_run_filters(tmp_path, capsys) -> None:
    agent = _agent(tmp_path)
    _seed_usage(
        agent,
        [
            {"run_id": "r1", "model": "m", "calls": 1, "input_tokens": 1,
             "output_tokens": 1, "total_tokens": 2, "timestamp": "2026-09-14T10:00:00"},
            {"run_id": "r2", "model": "m", "calls": 1, "input_tokens": 1,
             "output_tokens": 1, "total_tokens": 2, "timestamp": "2026-09-14T11:00:00"},
        ],
    )

    summary = _print_usage_summary(agent, run_id="r1")
    assert summary["calls"] == 1
    assert summary["run_id"] == "r1"

    _handle_usage_command("bogus-period", agent)
    out = capsys.readouterr().out
    assert "Unknown period" in out


def test_usage_handler_empty_state(tmp_path, capsys) -> None:
    agent = _agent(tmp_path)
    _handle_usage_command("", agent)
    out = capsys.readouterr().out
    assert "No usage recorded" in out


# --------------------------------------------------------------------------- #
# /audit slash handler
# --------------------------------------------------------------------------- #


def test_audit_handler_lists_decisions(tmp_path, capsys) -> None:
    agent = _agent(tmp_path)
    agent.permission_gate.audit.record(
        "tool_call", tool="shell", risk="dangerous", mode="ask",
        allowed=False, decided_by="policy", reason="unattended", arguments={},
    )
    agent.permission_gate.audit.record(
        "tool_call", tool="calculator", risk="safe", mode="ask",
        allowed=True, decided_by="policy", reason="safe", arguments={},
    )

    rows = _handle_audit_command("", agent)  # returns None; use table helper
    out = capsys.readouterr().out
    assert "shell" in out and "calculator" in out
    assert "denied" in out and "allowed" in out
    assert rows is None


def test_audit_denied_filter(tmp_path, capsys) -> None:
    agent = _agent(tmp_path)
    agent.permission_gate.audit.record(
        "tool_call", tool="shell", risk="dangerous", mode="ask",
        allowed=False, decided_by="policy", reason="unattended", arguments={},
    )
    agent.permission_gate.audit.record(
        "tool_call", tool="calculator", risk="safe", mode="ask",
        allowed=True, decided_by="policy", reason="safe", arguments={},
    )

    _handle_audit_command("--denied", agent)
    out = capsys.readouterr().out
    assert "shell" in out
    assert "calculator" not in out


def test_audit_disabled_prints_notice(tmp_path, capsys) -> None:
    agent = _agent(tmp_path, audit_log_enabled=False)
    _handle_audit_command("", agent)
    out = capsys.readouterr().out
    assert "Audit log is disabled" in out


# --------------------------------------------------------------------------- #
# Typer commands
# --------------------------------------------------------------------------- #


def test_usage_command_json_output(runner: CliRunner) -> None:
    result = _invoke(runner, "usage", "--json")
    # Empty state exits cleanly even in --json mode
    assert result.exit_code == 0, result.output


def test_audit_command_group_registered(runner: CliRunner) -> None:
    result = _invoke(runner, "--help")
    assert result.exit_code == 0
    assert "usage" in result.output
    assert "audit" in result.output
