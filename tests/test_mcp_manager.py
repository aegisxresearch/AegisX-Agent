"""MCPManager: config persistence, connect/disconnect lifecycle, and gating."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from mcp_compat import needs_demo_server
from support import run

from aegisx_agent.mcp.manager import (
    MCP_CONFIG_FILE,
    MCPManager,
    MCPManagerError,
    load_mcp_config,
    save_mcp_config,
)
from aegisx_agent.security.permissions import PermissionMode
from aegisx_agent.tools.base import ToolStatus
from aegisx_agent.tools.registry import ToolRegistry

SERVER_SCRIPT = str(Path(__file__).resolve().parent / "mcp_demo_server.py")


def _server_config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {"command": sys.executable, "args": [SERVER_SCRIPT]}
    config.update(overrides)
    return config


def _manager(tmp_path: Path, gate: Any = None) -> MCPManager:
    from aegisx_agent.plugins.registry import PluginRegistry

    return MCPManager(
        data_path=tmp_path, plugin_registry=PluginRegistry(), tool_registry=ToolRegistry(gate)
    )


# --------------------------------------------------------------------------- #
# Config persistence
# --------------------------------------------------------------------------- #


def test_missing_config_file_loads_as_empty(tmp_path: Path) -> None:
    assert load_mcp_config(tmp_path / MCP_CONFIG_FILE) == {}


def test_round_trip_preserves_servers(tmp_path: Path) -> None:
    path = tmp_path / MCP_CONFIG_FILE
    save_mcp_config(path, {"files": _server_config()})
    assert load_mcp_config(path) == {"files": _server_config()}


def test_invalid_json_is_reported_not_swallowed(tmp_path: Path) -> None:
    path = tmp_path / MCP_CONFIG_FILE
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(MCPManagerError, match="not valid JSON"):
        load_mcp_config(path)


def test_config_without_servers_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / MCP_CONFIG_FILE
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(MCPManagerError, match="mcpServers"):
        load_mcp_config(path)


def test_unknown_config_keys_fail_validation(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    with pytest.raises(MCPManagerError, match="unknown config keys"):
        manager.validate_server_configs({"srv": {"command": "x", "chmod": True}})


def test_missing_command_fails_validation(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    with pytest.raises(MCPManagerError, match="no 'command'"):
        manager.validate_server_configs({"srv": {"args": ["a"]}})


def test_persist_and_remove_round_trip(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.persist_server("files", _server_config())
    assert manager.get_server_config("files") == _server_config()
    assert manager.remove_server_config("files")
    assert not manager.remove_server_config("files")  # already gone
    with pytest.raises(MCPManagerError, match="not configured"):
        manager.get_server_config("files")


def test_persist_rejects_invalid_configs(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    with pytest.raises(MCPManagerError, match="unknown config keys"):
        manager.persist_server("bad", {"command": "x", "nope": 1})


# --------------------------------------------------------------------------- #
# Connect / disconnect lifecycle (real demo server process)
# --------------------------------------------------------------------------- #


@needs_demo_server
def test_connect_registers_gated_tools(tmp_path: Path) -> None:
    registry = ToolRegistry()
    plugins = _manager(tmp_path)._plugin_registry  # noqa: SLF001 - asserting wiring
    manager = MCPManager(tmp_path, plugins, registry)
    names = run(manager.connect_server("demo", _server_config()))

    assert names == [
        "plugin_mcp_demo_echo",
        "plugin_mcp_demo_explode",
        "plugin_mcp_demo_shouty_echo",
    ]
    assert registry.get("plugin_mcp_demo_echo") is not None
    assert manager.is_connected("demo")
    assert manager.bridged_plugin_ids() == ["mcp_demo"]

    run(manager.disconnect_server("demo"))
    assert registry.get("plugin_mcp_demo_echo") is None
    assert not manager.is_connected("demo")


@needs_demo_server
def test_double_connect_is_rejected(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    run(manager.connect_server("demo", _server_config()))
    try:
        with pytest.raises(MCPManagerError, match="already connected"):
            run(manager.connect_server("demo", _server_config()))
    finally:
        run(manager.disconnect_server("demo"))


@needs_demo_server
def test_failed_connect_leaves_no_half_registered_tools(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    with pytest.raises(MCPManagerError):
        # An invalid risk raises in the bridge — after the handshake, before
        # registration, proving nothing half-registered survives a failure.
        run(manager.connect_server("demo", _server_config(risk="yolo")))
    assert not manager.is_connected("demo")
    assert manager.bridged_plugin_ids() == []


def test_disconnect_unknown_server_returns_false(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    assert not run(manager.disconnect_server("ghost"))


@needs_demo_server
def test_close_all_tears_everything_down(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    run(manager.connect_server("demo", _server_config()))
    run(manager.close_all())
    assert manager.connected_servers() == []


# --------------------------------------------------------------------------- #
# Gating (the important part: MCP tools are NOT a permission side door)
# --------------------------------------------------------------------------- #


def _gated_agent_tools(tmp_path: Path, mode: PermissionMode, **overrides: Any) -> ToolRegistry:
    from aegisx_agent.security.audit import AuditLog
    from aegisx_agent.security.permissions import PermissionGate

    gate = PermissionGate(
        mode=mode,
        interactive=False,
        audit=AuditLog(tmp_path / "audit.log", enabled=False),
    )
    registry = ToolRegistry(gate)
    plugins = _manager(tmp_path)._plugin_registry  # noqa: SLF001
    manager = MCPManager(tmp_path, plugins, registry)
    run(manager.connect_server("demo", _server_config(**overrides)))
    return registry


@needs_demo_server
def test_caution_tools_run_in_ask_mode_unattended(tmp_path: Path) -> None:
    registry = _gated_agent_tools(tmp_path, PermissionMode.ASK)
    result = run(registry.execute("plugin_mcp_demo_echo", {"text": "hi"}))

    assert result.is_success
    assert result.output == "hi"


@needs_demo_server
def test_mcp_tools_are_refused_in_read_only_mode(tmp_path: Path) -> None:
    registry = _gated_agent_tools(tmp_path, PermissionMode.READ_ONLY)
    result = run(registry.execute("plugin_mcp_demo_echo", {"text": "hi"}))

    assert result.status is ToolStatus.ERROR
    assert "Permission denied" in (result.error or "")


@needs_demo_server
def test_dangerous_tools_are_denied_unattended(tmp_path: Path) -> None:
    # The demo server's own description carries no dangerous hints, so the
    # override makes the escalation explicit instead of incidental.
    registry = _gated_agent_tools(
        tmp_path, PermissionMode.ASK, tool_risks={"explode": "dangerous"}
    )
    result = run(registry.execute("plugin_mcp_demo_explode", {}))

    assert result.status is ToolStatus.ERROR
    assert "Permission denied" in (result.error or "")
