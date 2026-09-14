"""MCP tool bridge: name sanitization, risk resolution, and manifest building."""

from __future__ import annotations

from typing import Any

import pytest

from aegisx_agent.mcp.client import MCPToolClient, MCPToolSpec
from aegisx_agent.mcp.tools import (
    SUPPORTED_SERVER_CONFIG_KEYS,
    MCPBridgeError,
    MCPToolBridge,
    sanitize_tool_name,
)
from aegisx_agent.tools.base import ToolRisk

# --------------------------------------------------------------------------- #
# sanitize_tool_name
# --------------------------------------------------------------------------- #


def test_sanitize_collapses_disallowed_characters() -> None:
    assert sanitize_tool_name("My Tool.v2") == "my_tool_v2"
    assert sanitize_tool_name("  Spaced--Name  ") == "spaced--name"


def test_sanitize_guarantees_a_leading_letter() -> None:
    assert sanitize_tool_name("42engine") == "tool_42engine"
    assert sanitize_tool_name("---") == "tool"


def test_sanitize_caps_at_64_characters() -> None:
    assert len(sanitize_tool_name("x" * 200)) == 64


# --------------------------------------------------------------------------- #
# Risk resolution
# --------------------------------------------------------------------------- #


def _spec(name: str, description: str = "A remote tool.") -> MCPToolSpec:
    return MCPToolSpec(
        name=name,
        description=description,
        parameters={"type": "object", "properties": {}},
        server_id="srv",
    )


def _client(server_id: str = "srv") -> MCPToolClient:
    return MCPToolClient(server_id, {"command": "unused"})


def test_default_risk_is_caution_for_external_code() -> None:
    definitions = MCPToolBridge().build_definitions(
        "srv", {"command": "x"}, [_spec("read_thing")], _client()
    )

    assert definitions[0].manifest.risk is ToolRisk.CAUTION


def test_description_hints_escalate_to_dangerous() -> None:
    definitions = MCPToolBridge().build_definitions(
        "srv",
        {"command": "x"},
        [_spec("nuke_all", description="Destructive: deletes everything.")],
        _client(),
    )

    assert definitions[0].manifest.risk is ToolRisk.DANGEROUS


def test_per_tool_risk_overrides_beat_everything() -> None:
    definitions = MCPToolBridge().build_definitions(
        "srv",
        {
            "command": "x",
            "risk": "dangerous",
            "tool_risks": {"read_thing": "safe"},
        },
        [_spec("read_thing"), _spec("nuke_all", description="Destructive.")],
        _client(),
    )

    risks = {d.manifest.tool_name: d.manifest.risk for d in definitions}
    assert risks["read_thing"] is ToolRisk.SAFE  # per-tool beats hint + server default
    assert risks["nuke_all"] is ToolRisk.DANGEROUS


def test_invalid_risk_values_fail_loudly() -> None:
    with pytest.raises(MCPBridgeError, match="Invalid risk"):
        MCPToolBridge().build_definitions(
            "srv", {"command": "x", "risk": "yolo"}, [_spec("t")], _client()
        )
    with pytest.raises(MCPBridgeError, match="Invalid tool_risks entry"):
        MCPToolBridge().build_definitions(
            "srv", {"command": "x", "tool_risks": {"t": "meh"}}, [_spec("t")], _client()
        )


def test_policy_is_read_only_hostile_and_gate_remains_authoritative() -> None:
    definitions = MCPToolBridge().build_definitions(
        "srv", {"command": "x"}, [_spec("t")], _client()
    )

    manifest = definitions[0].manifest
    assert manifest.permission.allow_in_read_only is False
    # MCP tools may not claim SAFE in read-only mode: their effective risk is
    # at least CAUTION no matter what the config says.
    assert manifest.permission.effective_risk(ToolRisk.SAFE) is ToolRisk.CAUTION


# --------------------------------------------------------------------------- #
# Manifest building
# --------------------------------------------------------------------------- #


def test_definitions_share_the_server_plugin_id_and_qualify_tool_names() -> None:
    definitions = MCPToolBridge().build_definitions(
        "my-server", {"command": "x"}, [_spec("alpha"), _spec("beta")], _client()
    )

    plugin_ids = {d.manifest.plugin_id for d in definitions}
    assert plugin_ids == {"mcp_my-server"}  # '-' is legal in plugin ids
    assert [d.manifest.qualified_tool_name for d in definitions] == [
        "plugin_mcp_my-server_alpha",
        "plugin_mcp_my-server_beta",
    ]


def test_server_version_wins_over_handshake_and_bad_versions_fall_back() -> None:
    definitions = MCPToolBridge().build_definitions(
        "srv", {"command": "x", "version": "2.5.1"}, [_spec("t")], _client("srv-versioned")
    )
    assert definitions[0].manifest.version == "2.5.1"

    definitions = MCPToolBridge().build_definitions(
        "srv", {"command": "x"}, [_spec("t")], _client()  # handshake reports nothing
    )
    assert definitions[0].manifest.version == "0.0.0"


def test_empty_catalog_is_rejected() -> None:
    with pytest.raises(MCPBridgeError, match="advertises no tools"):
        MCPToolBridge().build_definitions("srv", {"command": "x"}, [], _client())


def test_duplicate_names_after_sanitization_are_rejected() -> None:
    with pytest.raises(MCPBridgeError, match="duplicate tools"):
        MCPToolBridge().build_definitions(
            "srv", {"command": "x"}, [_spec("a b"), _spec("a_b")], _client()
        )


def test_definitions_validate_against_the_plugin_contract() -> None:
    definitions = MCPToolBridge().build_definitions(
        "srv", {"command": "x"}, [_spec("echo_tool")], _client()
    )

    for definition in definitions:
        definition.validate()  # must not raise


def test_handler_forwarding_includes_errors_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeResult:
        def __init__(self, output: str, error: str | None = None) -> None:
            self.output = output
            self.error = error

        @property
        def is_success(self) -> bool:
            return self.error is None

    class FakeClient:
        server_version = ""

        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            calls.append((name, arguments))
            if name == "explode":
                return FakeResult("", error="boom")
            return FakeResult(f"ran {name} {arguments}")

    client = FakeClient()
    definitions = MCPToolBridge().build_definitions(
        "srv",
        {"command": "x"},
        [_spec("greet"), _spec("explode")],
        client,  # type: ignore[arg-type]
    )

    import asyncio

    greet = asyncio.run(definitions[0].handler(text="hi"))
    assert greet == "ran greet {'text': 'hi'}"

    with pytest.raises(MCPBridgeError, match="boom"):
        asyncio.run(definitions[1].handler())

    assert calls == [("greet", {"text": "hi"}), ("explode", {})]


def test_supported_config_keys_are_documented() -> None:
    assert SUPPORTED_SERVER_CONFIG_KEYS == frozenset(
        {"command", "args", "env", "risk", "tool_risks", "requires_approval", "version"}
    )  # 'version' bridges the handshake version when the server omits it
