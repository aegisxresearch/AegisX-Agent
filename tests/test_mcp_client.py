"""MCPToolClient against a real MCP server subprocess (stdio transport).

Every test here launches the packaged ``tests/mcp_demo_server.py`` over
real JSON-RPC stdio — the handshake, discovery, and tool calls are the
protocol itself, not a mock.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
from support import run

from aegisx_agent.mcp.client import (
    MCP_AVAILABLE,
    MCPClientError,
    MCPToolClient,
)
from aegisx_agent.tools.base import ToolStatus

pytestmark = pytest.mark.skipif(not MCP_AVAILABLE, reason="the 'mcp' package is not installed")

SERVER_SCRIPT = str(Path(__file__).resolve().parent / "mcp_demo_server.py")


def _client(server_id: str = "demo", **overrides: Any) -> MCPToolClient:
    config: dict[str, Any] = {"command": sys.executable, "args": [SERVER_SCRIPT]}
    config.update(overrides)
    return MCPToolClient(server_id, config)


def test_handshake_reports_the_server_identity() -> None:
    client = _client()
    try:
        specs = run(client.list_tools())

        assert client.connected
        assert client.server_info is not None
        assert client.server_info.name == "aegisx-mcp-demo"
        assert client.server_version == "1.4.2"
        assert [spec.name for spec in specs] == ["echo", "explode", "shouty_echo"]
    finally:
        run(client.close())


def test_tool_call_returns_output_and_server_error_becomes_error_result() -> None:
    client = _client()
    try:
        ok = run(client.call_tool("echo", {"text": "hello"}))
        assert ok.is_success
        assert ok.output == "hello"
        assert ok.metadata["server"] == "aegisx-mcp-demo"

        failure = run(client.call_tool("explode", {}))
        assert failure.status is ToolStatus.ERROR
        # mcp 2.x servers wrap tool exceptions in a generic message; the
        # traceback goes to the server's stderr, not the protocol response.
        assert "Error executing tool explode" in (failure.error or "")
    finally:
        run(client.close())


def test_concurrent_calls_serialize_on_one_connection() -> None:
    client = _client()
    try:
        run(client.list_tools())  # force the connection up first

        async def _all() -> list[str]:
            results = await asyncio.gather(
                *(client.call_tool("echo", {"text": f"m{i}"}) for i in range(4))
            )
            return [result.output for result in results]

        assert run(_all()) == [f"m{i}" for i in range(4)]
    finally:
        run(client.close())


def test_missing_command_is_a_clean_error() -> None:
    client = MCPToolClient("broken", {"command": "   "})
    with pytest.raises(MCPClientError, match="no 'command' configured"):
        run(client.list_tools())
    assert not client.connected


def test_unlaunchable_command_reports_the_os_error() -> None:
    client = _client("ghost", command="/no/such/binary-xyz")
    with pytest.raises(MCPClientError, match="Failed to start MCP server 'ghost'"):
        run(client.list_tools())
    assert not client.connected


def test_close_is_idempotent_and_discovery_normalizes_schemas() -> None:
    client = _client()
    specs = run(client.list_tools())
    shouty = next(spec for spec in specs if spec.name == "shouty_echo")
    # The demo server omits both description and type — the client normalizes.
    assert shouty.description
    assert shouty.parameters.get("type") == "object"
    run(client.close())
    assert not client.connected
    run(client.close())  # second close is a no-op, not a crash
