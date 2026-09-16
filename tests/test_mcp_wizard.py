"""The interactive `mcp add` wizard: catalog pick, placeholder fill, auto test."""

from __future__ import annotations

import asyncio
from typing import Any

from aegisx_agent.cli.commands import mcp as mcp_cmds
from aegisx_agent.mcp.client import MCPClientError
from aegisx_agent.mcp.manager import MCPManagerError


class _FakeMCP:
    """Manager stand-in that records persists and can force connect failures."""

    def __init__(self, fail: bool = False) -> None:
        self.saved: dict[str, dict[str, Any]] = {}
        self.fail = fail
        self.config_path = "/tmp/fake_mcp_servers.json"

    def persist_server(self, server_id: str, server_config: dict[str, Any]) -> None:
        self.saved[server_id] = server_config

    def get_server_config(self, server_id: str) -> dict[str, Any]:
        if server_id not in self.saved:
            raise MCPManagerError(f"MCP server '{server_id}' is not configured")
        return self.saved[server_id]


class _FakeAgent:
    def __init__(self, fail: bool = False) -> None:
        self.mcp = _FakeMCP(fail)
        self.tools = type("T", (), {"get": staticmethod(lambda name: None)})()

    async def connect_mcp_server(
        self, server_id: str, server_config: dict[str, Any] | None = None
    ) -> list[str]:
        if self.mcp.fail:
            raise MCPClientError("handshake: connection closed")
        return [f"mcp_{server_id}_tool"]


def _run_wizard(agent: Any, answers: list[str]) -> str:
    """Drive the wizard with canned Prompt answers; return captured output."""

    answer_iter = iter(answers)

    class FakePrompt:
        @staticmethod
        def ask(*args: Any, **kwargs: Any) -> str:
            return next(answer_iter)

    # The wizard imports rich.prompt.Prompt inside the function, and rich's
    # Console is bound into the commands module at import time — swap both.
    import io

    import rich.prompt
    from rich.console import Console

    from aegisx_agent.cli.commands import mcp as mcp_mod

    buffer = io.StringIO()
    quiet = Console(file=buffer, force_terminal=False, width=200)
    original = rich.prompt.Prompt
    original_mcp_console = mcp_mod.console
    rich.prompt.Prompt = FakePrompt
    mcp_mod.console = quiet
    try:
        mcp_cmds._mcp_wizard(agent)
        return buffer.getvalue()
    finally:
        rich.prompt.Prompt = original
        mcp_mod.console = original_mcp_console


def test_wizard_catalog_pick_fills_placeholders_and_connects() -> None:
    agent = _FakeAgent()

    # "2" = filesystem in the sorted catalog (has <allowed-dir>), then the dir.
    out = _run_wizard(agent, ["2", "/tmp/project"])

    assert "Saved 'filesystem'" in out
    assert agent.mcp.saved["filesystem"]["command"] == "npx"
    assert agent.mcp.saved["filesystem"]["args"] == [
        "-y",
        "@modelcontextprotocol/server-filesystem",
        "/tmp/project",
    ]
    assert "Connected" in out
    assert "mcp_filesystem_tool" in out


def test_wizard_catalog_entry_with_env_collects_secrets() -> None:
    agent = _FakeAgent()
    entries = sorted(__import__("aegisx_agent.mcp.catalog", fromlist=["CATALOG"]).CATALOG)
    github_index = str(entries.index("github") + 1)

    # Pick github, fill <your-token> placeholder… wait: token is an env hint.
    out = _run_wizard(agent, [github_index, "secret-token"])

    assert "Saved 'github'" in out
    assert agent.mcp.saved["github"]["env"] == {"GITHUB_TOKEN": "secret-token"}


def test_wizard_manual_entry_saves_without_connecting_args() -> None:
    agent = _FakeAgent()

    # "m" manual → id, command, args.
    out = _run_wizard(agent, ["m", "mydb", "uvx", "mcp-server-sqlite --db x.db"])

    assert "Saved 'mydb'" in out
    assert agent.mcp.saved["mydb"] == {
        "command": "uvx",
        "args": ["mcp-server-sqlite", "--db", "x.db"],
    }
    assert "Testing connection" in out


def test_wizard_connection_failure_keeps_config_and_advises() -> None:
    agent = _FakeAgent(fail=True)

    out = _run_wizard(agent, ["m", "broken", "no-such-binary-xyz", ""])

    assert "Saved 'broken'" in out  # config persisted
    assert "Connection test failed" in out
    assert "mcp connect broken" in out  # recovery hint
    assert "mcp doctor broken" in out


def test_wizard_blank_placeholder_aborts() -> None:
    agent = _FakeAgent()

    out = _run_wizard(agent, ["2", ""])

    assert "value is required" in out
    assert agent.mcp.saved == {}


def test_wizard_blank_manual_id_aborts() -> None:
    agent = _FakeAgent()

    out = _run_wizard(agent, ["m", ""])

    assert "server id is required" in out
    assert agent.mcp.saved == {}


def test_event_loop_not_closed_after_wizard() -> None:
    """The wizard runs connect via asyncio.run; ensure a second call works."""
    agent = _FakeAgent()
    _run_wizard(agent, ["m", "a", "cmd", ""])
    _run_wizard(agent, ["m", "b", "cmd", ""])
    asyncio.run(asyncio.sleep(0))
