"""Tool registry: every failure mode must return a ToolResult, never raise."""

from __future__ import annotations

from support import EchoTool, ExplodingTool, SchemalessTool, run

from aegisx_agent.tools.base import ToolStatus
from aegisx_agent.tools.registry import ToolRegistry


def test_unknown_tool_returns_error_result() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())

    result = run(registry.execute("ghost", {}))

    assert result.status is ToolStatus.ERROR
    assert "not found" in (result.error or "")
    assert "echo" in (result.error or "")


def test_json_string_arguments_are_parsed() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())

    result = run(registry.execute("echo", '{"text": "hi"}'))

    assert result.is_success
    assert result.output == "echo:hi"


def test_malformed_json_reports_expected_parameters() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())

    result = run(registry.execute("echo", "{not json"))

    assert result.status is ToolStatus.ERROR
    assert "not valid JSON" in (result.error or "")
    assert "text" in (result.error or "")


def test_unknown_parameter_is_rejected_without_raising() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())

    result = run(registry.execute("echo", {"text": "hi", "input": "hi"}))

    assert result.status is ToolStatus.ERROR
    assert "Unknown parameter" in (result.error or "")


def test_crashing_tool_is_contained() -> None:
    registry = ToolRegistry()
    registry.register(ExplodingTool())

    result = run(registry.execute("boom", {}))

    assert result.status is ToolStatus.ERROR
    assert "RuntimeError" in (result.error or "")
    assert "kaboom" in (result.error or "")


def test_schemaless_tool_accepts_arbitrary_kwargs() -> None:
    registry = ToolRegistry()
    registry.register(SchemalessTool())

    result = run(registry.execute("raw", {"anything": 1}))

    assert result.is_success
    assert result.output == "raw:['anything']"


def test_schemas_and_listing_are_available() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())

    assert [tool.name for tool in registry.list_tools()] == ["echo"]
    assert registry.get("echo") is not None
    assert registry.list_schemas()[0]["function"]["name"] == "echo"

    registry.unregister("echo")
    assert registry.get("echo") is None
