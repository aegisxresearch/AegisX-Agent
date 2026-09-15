"""CLI surface for /usage and /audit plus agent-side tracker wiring."""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from aegisx_agent.cli import main as cli
from aegisx_agent.cli.commands.observability import (
    _format_seconds,
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
# /usage subagent breakdown
# --------------------------------------------------------------------------- #


def _seed_delegated_usage(agent: AegisXAgent) -> None:
    """One parent turn plus two delegations, each with a closing summary row."""
    _seed_usage(
        agent,
        [
            {"run_id": "r1", "model": "m", "calls": 1, "input_tokens": 100,
             "output_tokens": 20, "total_tokens": 120, "timestamp": "2026-09-14T10:00:00"},
            {"run_id": "r1", "model": "m", "calls": 1, "input_tokens": 10,
             "output_tokens": 5, "total_tokens": 15, "timestamp": "2026-09-14T10:01:00",
             "delegation": "abc12345", "depth": 1, "task": "summarise the logs"},
            {"run_id": "r1", "timestamp": "2026-09-14T10:01:30", "event": "delegation",
             "delegation": "abc12345", "depth": 1, "task": "summarise the logs",
             "steps": 2, "tool_calls": 1, "duration_seconds": 4.5, "status": "completed"},
            {"run_id": "r1", "model": "m", "calls": 2, "input_tokens": 40,
             "output_tokens": 5, "total_tokens": 45, "timestamp": "2026-09-14T10:02:00",
             "delegation": "def67890", "depth": 2, "task": "count the errors"},
            {"run_id": "r1", "timestamp": "2026-09-14T10:04:00", "event": "delegation",
             "delegation": "def67890", "depth": 2, "task": "count the errors",
             "steps": 3, "tool_calls": 2, "duration_seconds": 125.0, "status": "budget"},
        ],
    )


@pytest.fixture()
def wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the render width: rich reads COLUMNS, so tables never truncate."""
    monkeypatch.setenv("COLUMNS", "120")


def test_usage_summary_breaks_subagent_cost_out_per_delegation(
    tmp_path, capsys, wide_console
) -> None:
    agent = _agent(tmp_path)
    _seed_delegated_usage(agent)

    summary = _print_usage_summary(agent)
    out = capsys.readouterr().out

    assert summary["total_tokens"] == 180
    assert summary["subagent_tokens"] == 60
    assert summary["parent_tokens"] == 120
    assert summary["subagent_calls"] == 3
    # Most expensive delegation first, each with its own task label.
    assert [item["delegation"] for item in summary["delegations"]] == ["def67890", "abc12345"]
    assert [item["depth"] for item in summary["delegations"]] == [2, 1]

    assert "Subagent tokens" in out and "Parent tokens" in out
    assert "33.3%" in out  # 60 of 180 tokens
    assert "Per delegation" in out
    assert "def67890" in out and "count the errors" in out
    assert "abc12345" in out and "summarise the logs" in out
    # A delegation summary is not a call: it must not reach the per-model table.
    assert "unknown" not in out


def test_usage_delegations_run_view_lists_steps_and_duration(
    tmp_path, capsys, wide_console
) -> None:
    agent = _agent(tmp_path)
    _seed_delegated_usage(agent)

    summary = _print_usage_summary(agent, run_id="r1", delegations_only=True)
    out = capsys.readouterr().out

    assert "(run r1)" in out
    assert all(header in out for header in ("Steps", "Calls", "Tokens", "Duration", "Status"))

    # Each delegation carries the outcome its closing summary row recorded.
    by_id = {item["delegation"]: item for item in summary["delegations"]}
    assert by_id["abc12345"]["steps"] == 2
    assert by_id["abc12345"]["tool_calls"] == 1
    assert by_id["abc12345"]["duration_seconds"] == 4.5
    assert by_id["abc12345"]["status"] == "completed"
    assert by_id["def67890"]["steps"] == 3
    assert by_id["def67890"]["duration_seconds"] == 125.0
    assert by_id["def67890"]["status"] == "budget"

    assert "4.5s" in out and "2m 05s" in out  # sub-minute and sub-hour spellings
    assert "completed" in out and "budget" in out


def test_delegation_without_a_summary_row_degrades_gracefully(
    tmp_path, capsys, wide_console
) -> None:
    agent = _agent(tmp_path)
    # A log written before summary rows existed: tokens only, no steps/time.
    _seed_usage(
        agent,
        [{"run_id": "r1", "model": "m", "calls": 2, "input_tokens": 10,
          "output_tokens": 5, "total_tokens": 15, "timestamp": "2026-09-14T10:00:00",
          "delegation": "abc12345", "depth": 1, "task": "old delegation"}],
    )

    summary = _print_usage_summary(agent, delegations_only=True)
    out = capsys.readouterr().out

    assert summary["delegations"][0]["steps"] == 0
    assert summary["delegations"][0]["duration_seconds"] == 0.0
    # Em dashes, not a misleading 0.0s / 0 steps.
    assert "—" in out
    assert "running?" in out


def test_format_seconds_covers_every_magnitude() -> None:
    assert _format_seconds(0) == "—"
    assert _format_seconds(0.5) == "0.5s"
    assert _format_seconds(59.9) == "59.9s"
    assert _format_seconds(60) == "1m 00s"
    assert _format_seconds(125.0) == "2m 05s"
    assert _format_seconds(3600) == "1h 00m"
    assert _format_seconds(7325) == "2h 02m"


def test_usage_summary_without_delegations_has_no_subagent_rows(tmp_path, capsys) -> None:
    agent = _agent(tmp_path)
    _seed_usage(
        agent,
        [{"run_id": "r1", "model": "m", "calls": 1, "input_tokens": 1,
          "output_tokens": 1, "total_tokens": 2, "timestamp": "2026-09-14T10:00:00"}],
    )

    summary = _print_usage_summary(agent)
    out = capsys.readouterr().out

    assert summary["delegations"] == []
    assert summary["subagent_tokens"] == 0
    assert "Subagent" not in out
    assert "Per delegation" not in out


def test_delegations_flag_shows_only_subagent_work(tmp_path, capsys, wide_console) -> None:
    agent = _agent(tmp_path)
    _seed_delegated_usage(agent)

    summary = _print_usage_summary(agent, delegations_only=True)
    out = capsys.readouterr().out

    # The parent's 120 tokens are filtered out, so the split is 100% subagent.
    assert summary["total_tokens"] == 60
    assert summary["parent_tokens"] == 0
    assert summary["delegations_only"] is True
    assert "Filtered to subagent work only" in out
    assert "100.0%" in out
    assert "summarise the logs" in out


def test_usage_handler_accepts_the_delegations_flag(tmp_path, capsys) -> None:
    agent = _agent(tmp_path)
    _seed_delegated_usage(agent)

    _handle_usage_command("--delegations", agent)
    out = capsys.readouterr().out
    assert "Filtered to subagent work only" in out
    assert "Per delegation" in out


def test_delegation_table_caps_rows_and_reports_the_tail(tmp_path, capsys, wide_console) -> None:
    agent = _agent(tmp_path)
    _seed_usage(
        agent,
        [
            {"run_id": "r1", "model": "m", "calls": 1, "input_tokens": 1,
             "output_tokens": 0, "total_tokens": 10 + index, "timestamp": "2026-09-14T10:00:00",
             "delegation": f"d{index:07d}", "depth": 1, "task": f"job {index}"}
            for index in range(13)
        ],
    )

    summary = _print_usage_summary(agent, delegations_only=True)
    out = capsys.readouterr().out

    assert len(summary["delegations"]) == 13
    assert "job 12" in out  # the priciest delegation is still shown
    assert "job 0" not in out  # the tail is summarized, not printed
    assert "3 more delegation(s)" in out


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


def test_usage_command_accepts_delegations_flag(runner: CliRunner) -> None:
    result = _invoke(runner, "usage", "--delegations")
    assert result.exit_code == 0, result.output

    help_text = _invoke(runner, "usage", "--help").output
    assert "--delegations" in help_text


def test_audit_command_group_registered(runner: CliRunner) -> None:
    result = _invoke(runner, "--help")
    assert result.exit_code == 0
    assert "usage" in result.output
    assert "audit" in result.output
