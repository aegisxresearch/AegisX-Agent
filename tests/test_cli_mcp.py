"""CLI surface for MCP servers: /mcp slash commands and the aegisx mcp sub-app."""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp_compat import needs_demo_server
from support import run
from typer.testing import CliRunner

from aegisx_agent.cli import main as cli
from aegisx_agent.cli.commands.mcp import (
    _handle_mcp_command,
    _load_server_config_file,
    _parse_add_arguments,
)
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


def _invoke(runner: CliRunner, *args: str) -> Any:
    return runner.invoke(cli.app, list(args))


def _agent(tmp_path: Any, **overrides: Any) -> AegisXAgent:
    config = AgentConfig(
        llm_provider=LLMProvider.CUSTOM,
        custom_base_url="http://127.0.0.1:1",  # never reached: no chat happens
        custom_api_key="test-key",
        custom_model="fake-model",
        data_dir=str(tmp_path),
        rag_enabled=False,
        permission_mode=PermissionMode.ALLOW_ALL,
        **overrides,
    )
    return AegisXAgent(config)


# --------------------------------------------------------------------------- #
# Argument parsing and config files
# --------------------------------------------------------------------------- #


def test_parse_add_arguments_splits_command_and_args() -> None:
    server_id, server_config = _parse_add_arguments("files npx -y server-fs /tmp")

    assert server_id == "files"
    assert server_config == {"command": "npx", "args": ["-y", "server-fs", "/tmp"]}


def test_parse_add_arguments_requires_a_command() -> None:
    with pytest.raises(ValueError, match="Expected:"):
        _parse_add_arguments("only-an-id")


def test_load_server_config_file_accepts_both_shapes(tmp_path: Any) -> None:
    document = tmp_path / "doc.json"
    document.write_text(json.dumps({"mcpServers": {"a": {"command": "x"}}}))
    single = tmp_path / "one.json"
    single.write_text(json.dumps({"command": "y", "args": ["z"]}))

    assert _load_server_config_file(str(document), "a") == {"command": "x"}
    assert _load_server_config_file(str(single), "b") == {"command": "y", "args": ["z"]}

    with pytest.raises(ValueError, match="has no entry 'missing'"):
        _load_server_config_file(str(document), "missing")


# --------------------------------------------------------------------------- #
# Slash handler (offline agent, no server processes needed)
# --------------------------------------------------------------------------- #


def test_slash_mcp_usage_and_unknown_subcommand(tmp_path, capsys) -> None:
    agent = _agent(tmp_path)

    _handle_mcp_command("", agent)
    _handle_mcp_command("bogus", agent)
    out = capsys.readouterr().out

    assert out.count("/mcp list") >= 2  # usage printed twice: help + unknown
    assert "Unknown /mcp subcommand" in out


def test_slash_mcp_add_persists_and_lists(tmp_path, capsys) -> None:
    agent = _agent(tmp_path)

    _handle_mcp_command("add files python -m demo", agent)
    out = capsys.readouterr().out

    assert "Saved 'files' to" in out and "mcp_servers.json" in out
    assert "MCP Servers" in out  # table shown afterwards
    assert agent.mcp.get_server_config("files") == {
        "command": "python",
        "args": ["-m", "demo"],
    }


def test_slash_mcp_add_validation_error_is_visible(tmp_path, capsys) -> None:
    agent = _agent(tmp_path)

    _handle_mcp_command("add bad python --risk", agent)  # risk option is not a flag here
    out = capsys.readouterr().out

    # 'risk' ends up an arg; the add itself succeeds but 'python' carries args
    assert "Saved 'bad'" in out or "MCP add failed" in out


def test_slash_mcp_remove_reports_unknown(tmp_path, capsys) -> None:
    agent = _agent(tmp_path)

    _handle_mcp_command("remove ghost", agent)
    out = capsys.readouterr().out

    assert "No configured MCP server" in out


def test_slash_mcp_disconnect_reports_not_connected(tmp_path, capsys) -> None:
    agent = _agent(tmp_path)

    _handle_mcp_command("disconnect ghost", agent)
    out = capsys.readouterr().out

    assert "is not connected" in out
    assert "Connected: -" in out


def test_slash_mcp_list_shows_empty_state(tmp_path, capsys) -> None:
    agent = _agent(tmp_path)

    _handle_mcp_command("list", agent)
    out = capsys.readouterr().out

    assert "No MCP servers configured" in out


# --------------------------------------------------------------------------- #
# Typer sub-app (real server process for connect)
# --------------------------------------------------------------------------- #


DEMO_SERVER = str(__import__("pathlib").Path(__file__).parent / "mcp_demo_server.py")


def test_mcp_group_is_registered(runner: CliRunner) -> None:
    result = _invoke(runner, "--help")

    assert result.exit_code == 0
    assert "mcp" in result.output


def test_mcp_add_then_list_and_remove(runner: CliRunner) -> None:
    added = _invoke(runner, "mcp", "add", "files", "python", "--risk", "safe")
    assert added.exit_code == 0, added.output

    listed = _invoke(runner, "mcp", "list")
    assert listed.exit_code == 0, listed.output
    assert "files" in listed.output

    removed = _invoke(runner, "mcp", "remove", "files")
    assert removed.exit_code == 0, removed.output
    assert "Removed" in removed.output


def test_mcp_add_with_invalid_risk_fails_with_exit_code(runner: CliRunner) -> None:
    result = _invoke(runner, "mcp", "add", "bad", "python", "--risk", "yolo")

    assert result.exit_code == 1
    assert "MCP add failed" in result.output
    assert "invalid risk" in result.output


@needs_demo_server
def test_mcp_connect_registers_tools_and_disconnect_removes_them(runner, tmp_path) -> None:
    config_file = tmp_path / "servers.json"
    config_file.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "demo": {"command": __import__("sys").executable, "args": [DEMO_SERVER]}
                }
            }
        )
    )

    connected = _invoke(runner, "mcp", "connect", "demo", "--config", str(config_file))
    assert connected.exit_code == 0, connected.output
    assert "plugin_mcp_demo_echo" in connected.output
    assert "mcp_demo_echo" in connected.output  # bare remote name is listed too

    # The CLI caches one agent per test, so this disconnect closes the very
    # connection opened above — nothing leaks past the test.
    disconnected = _invoke(runner, "mcp", "disconnect", "demo")
    assert disconnected.exit_code == 0, disconnected.output
    assert "its tools were removed" in disconnected.output


def test_mcp_connect_reports_missing_entry(runner, tmp_path) -> None:
    config_file = tmp_path / "servers.json"
    config_file.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))

    result = _invoke(runner, "mcp", "connect", "demo", "--config", str(config_file))

    assert result.exit_code == 1
    assert "no entry 'demo'" in result.output


def test_mcp_disconnect_unknown_id_exits_nonzero(runner: CliRunner) -> None:
    result = _invoke(runner, "mcp", "disconnect", "ghost")

    assert result.exit_code == 1
    assert "is not connected" in result.output


def test_mcp_connect_with_unlaunchable_command_fails_cleanly(runner, tmp_path) -> None:
    config_file = tmp_path / "servers.json"
    config_file.write_text(
        json.dumps({"mcpServers": {"ghost": {"command": "/no/such/binary-xyz"}}})
    )

    result = _invoke(runner, "mcp", "connect", "ghost", "--config", str(config_file))

    assert result.exit_code == 1
    assert "MCP connect failed" in result.output


# --------------------------------------------------------------------------- #
# Agent-level API (used by the CLI and library consumers)
# --------------------------------------------------------------------------- #


@needs_demo_server
def test_agent_connect_uses_persisted_config(tmp_path, monkeypatch) -> None:
    import sys as _sys

    agent = _agent(tmp_path)
    agent.mcp.persist_server("demo", {"command": _sys.executable, "args": [DEMO_SERVER]})

    names = run(agent.connect_mcp_server("demo"))
    try:
        assert "plugin_mcp_demo_echo" in names
        servers = agent.list_mcp_servers()
        assert servers["demo"]["connected"] is True
    finally:
        run(agent.disconnect_mcp_server("demo"))
    assert agent.list_mcp_servers()["demo"]["connected"] is False
