"""Provider wire-format conversion for tool calls and system prompts."""

from __future__ import annotations

import json

from aegisx_agent.llm.anthropic_provider import AnthropicProvider
from aegisx_agent.llm.base import Message, Role, ToolCall, normalise_tool_arguments


def _provider() -> AnthropicProvider:
    return AnthropicProvider(model="claude-test", api_key="test-key")


def test_anthropic_tool_call_arguments_are_valid_json() -> None:
    call = ToolCall.from_anthropic({"id": "t1", "name": "echo", "input": {"text": "hi"}})

    assert call.id == "t1"
    assert call.name == "echo"
    assert json.loads(call.arguments) == {"text": "hi"}


def test_normalise_tool_arguments_handles_every_shape() -> None:
    assert normalise_tool_arguments({"a": 1}) == '{"a": 1}'
    assert normalise_tool_arguments('{"a": 1}') == '{"a": 1}'
    assert normalise_tool_arguments(None) == "{}"
    assert normalise_tool_arguments("") == ""


def test_anthropic_joins_every_system_message() -> None:
    system, converted = _provider()._convert_messages(
        [
            Message(role=Role.SYSTEM, content="persona"),
            Message(role=Role.SYSTEM, content="[Agent Memory]: likes short answers"),
            Message(role=Role.USER, content="hi"),
        ]
    )

    assert system == "persona\n\n[Agent Memory]: likes short answers"
    assert converted == [{"role": "user", "content": "hi"}]


def test_anthropic_converts_tool_calls_and_merges_tool_results() -> None:
    _, converted = _provider()._convert_messages(
        [
            Message(role=Role.USER, content="echo hi"),
            Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "echo", "arguments": '{"text": "hi"}'},
                    }
                ],
            ),
            Message(role=Role.TOOL, content="echo:hi", tool_call_id="c1", name="echo"),
            Message(role=Role.TOOL, content="echo:ho", tool_call_id="c2", name="echo"),
        ]
    )

    assert len(converted) == 3
    assert converted[1] == {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "c1", "name": "echo", "input": {"text": "hi"}}],
    }
    assert converted[2] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": "echo:hi"},
            {"type": "tool_result", "tool_use_id": "c2", "content": "echo:ho"},
        ],
    }


def test_anthropic_keeps_assistant_text_before_tool_use() -> None:
    _, converted = _provider()._convert_messages(
        [
            Message(
                role=Role.ASSISTANT,
                content="let me check",
                tool_calls=[
                    {
                        "id": "c9",
                        "type": "function",
                        "function": {"name": "echo", "arguments": "not-json"},
                    }
                ],
            )
        ]
    )

    assert converted[0]["content"] == [
        {"type": "text", "text": "let me check"},
        {"type": "tool_use", "id": "c9", "name": "echo", "input": {}},
    ]


def test_ollama_provider_normalises_dict_arguments() -> None:
    from aegisx_agent.llm.ollama_provider import OllamaProvider

    provider = OllamaProvider(model="llama3.1")

    assert provider.model == "llama3.1"
    assert provider._convert_tools(
        [{"type": "function", "function": {"name": "echo", "description": "d", "parameters": {}}}]
    ) == [{"type": "function", "function": {"name": "echo", "description": "d", "parameters": {}}}]
