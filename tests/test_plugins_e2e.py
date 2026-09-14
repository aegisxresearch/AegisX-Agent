"""End to end: plugin tools execute through the real agentic loop.

A plugin call must travel the exact same path as a builtin tool — fake HTTP
server → agent loop → ToolRegistry → PermissionGate → plugin handler — so
these tests prove plugins are first-class citizens, not a side door.
"""

from __future__ import annotations

from typing import Any

import pytest
from fake_llm import FakeLLMServer, openai_text_response, openai_tool_call_response
from support import run

from aegisx_agent.config import AgentConfig, LLMProvider
from aegisx_agent.core import AegisXAgent
from aegisx_agent.plugins import (
    PluginManifest,
    PluginPermissionPolicy,
    define_plugin,
)
from aegisx_agent.security.permissions import PermissionMode


@pytest.fixture()
def fake_llm():
    server = FakeLLMServer()
    try:
        yield server
    finally:
        server.stop()


def _agent(fake_llm: FakeLLMServer, tmp_path: Any, **overrides: Any) -> AegisXAgent:
    config = AgentConfig(
        llm_provider=LLMProvider.CUSTOM,
        custom_base_url=fake_llm.base_url,
        custom_api_key="test-key",
        custom_model="fake-model",
        data_dir=str(tmp_path),
        rag_enabled=False,
        web_search_enabled=False,
        max_iterations=4,
        **overrides,
    )
    return AegisXAgent(config)


def _weather_plugin():
    manifest = PluginManifest(
        plugin_id="weather",
        version="1.0.0",
        tool_name="lookup",
        description="Look up the weather for a city.",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    )

    @define_plugin(manifest)
    def lookup(city: str) -> str:
        return f"weather:{city}=sunny"

    return lookup


def test_plugin_tool_call_round_trips_through_the_full_agent_loop(
    fake_llm: FakeLLMServer, tmp_path
) -> None:
    """LLM asks for the plugin tool; the loop executes it through the registry."""
    fake_llm.script(
        openai_tool_call_response(
            "call_plugin", "plugin_weather_lookup", '{"city": "Jakarta"}'
        ),
        openai_text_response("It is sunny in Jakarta."),
    )
    agent = _agent(fake_llm, tmp_path)
    name = agent.register_plugin(_weather_plugin())
    assert name == "plugin_weather_lookup"

    answer = run(agent.chat("what is the weather in Jakarta?"))

    assert answer == "It is sunny in Jakarta."
    assert len(fake_llm.requests) == 2

    # The plugin's versioned schema was offered to the LLM.
    _, first_request = fake_llm.requests[0]
    offered = [tool["function"]["name"] for tool in first_request["tools"]]
    assert "plugin_weather_lookup" in offered
    plugin_schema = next(
        tool["function"] for tool in first_request["tools"] if tool["function"]["name"] == name
    )
    assert plugin_schema["parameters"]["required"] == ["city"]

    # The plugin result reached the LLM as a normal tool message.
    _, second_request = fake_llm.requests[1]
    tool_message = second_request["messages"][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call_plugin"
    assert tool_message["content"] == "weather:Jakarta=sunny"


def test_a_dangerous_plugin_is_denied_in_read_only_mode_inside_the_loop(
    fake_llm: FakeLLMServer, tmp_path
) -> None:
    """The gate still refuses a gated plugin mid-loop; the LLM sees the denial."""
    manifest = PluginManifest(
        plugin_id="guarded",
        version="1.0.0",
        tool_name="read",
        description="Read guarded data.",
        permission=PluginPermissionPolicy(requires_approval=True),
    )

    @define_plugin(manifest)
    def read() -> str:
        return "should never run"

    fake_llm.script(
        openai_tool_call_response("call_guarded", "plugin_guarded_read", "{}"),
        # A denied tool triggers one recovery consultation first.
        openai_text_response(
            '{"analysis": "denied by policy", "fix_suggestion": "none", '
            '"alternative_tool": null, "alternative_args": null}'
        ),
        openai_text_response("I could not access that."),
    )
    agent = _agent(fake_llm, tmp_path, permission_mode=PermissionMode.READ_ONLY)
    agent.register_plugin(read)

    answer = run(agent.chat("read the guarded data"))

    assert answer == "I could not access that."

    # Three calls: tool request, recovery consultation, loop continuation.
    assert len(fake_llm.requests) == 3

    # The recovery turn is a plain consultation: no tools offered.
    assert "tools" not in fake_llm.requests[1][1]

    # The denial reached the LLM as a tool message on the continuation turn.
    _, continuation = fake_llm.requests[2]
    tool_message = continuation["messages"][-1]
    assert tool_message["role"] == "tool"
    assert "Permission denied" in tool_message["content"]
    assert "read-only mode" in tool_message["content"]


def test_unloading_a_plugin_removes_it_from_the_schema_offered_to_the_llm(
    fake_llm: FakeLLMServer, tmp_path
) -> None:
    fake_llm.script(openai_text_response("ok"))
    agent = _agent(fake_llm, tmp_path)
    agent.register_plugin(_weather_plugin())
    assert agent.unload_plugin("weather")

    run(agent.chat("hello"))

    _, request = fake_llm.requests[0]
    offered = [tool["function"]["name"] for tool in request["tools"]]
    assert "plugin_weather_lookup" not in offered


def test_plugin_loaded_from_a_file_executes_through_the_loop(
    fake_llm: FakeLLMServer, tmp_path
) -> None:
    plugin_file = tmp_path / "my_plugin.py"
    plugin_file.write_text(
        "from aegisx_agent.plugins import PluginManifest, define_plugin\n"
        "MANIFEST = PluginManifest(\n"
        "    plugin_id='fileplug', version='1.0.0', tool_name='ping',\n"
        "    description='Ping the plugin.',\n"
        ")\n"
        "PLUGIN = define_plugin(MANIFEST)(lambda: 'pong')\n",
        encoding="utf-8",
    )
    fake_llm.script(
        openai_tool_call_response("call_fp", "plugin_fileplug_ping", "{}"),
        openai_text_response("pong!"),
    )
    agent = _agent(fake_llm, tmp_path)
    names = agent.load_plugin_path(str(plugin_file))
    assert names == ["plugin_fileplug_ping"]

    answer = run(agent.chat("ping the plugin"))

    assert answer == "pong!"
    _, second_request = fake_llm.requests[1]
    assert second_request["messages"][-1]["content"] == "pong"

    import sys

    sys.modules.pop("aegisx_external_plugin_my_plugin", None)
