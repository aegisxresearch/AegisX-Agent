"""End-to-end: the agent completes real HTTP round trips against a fake provider.

Everything here goes through httpx and a live socket — no scripted provider —
so it covers the request/response plumbing that unit tests cannot reach.
"""

from __future__ import annotations

from typing import Any

import pytest
from fake_llm import (
    FakeLLMServer,
    anthropic_text_response,
    anthropic_tool_use_response,
    openai_text_response,
    openai_tool_call_response,
    sse_text,
    too_many_requests,
)
from support import run

from aegisx_agent.config import AgentConfig, LLMProvider
from aegisx_agent.core import AegisXAgent
from aegisx_agent.llm.anthropic_provider import AnthropicProvider
from aegisx_agent.llm.base import Message, Role
from aegisx_agent.llm.custom_provider import CustomProvider


async def _collect(iterator: Any) -> list[str]:
    return [chunk async for chunk in iterator]


@pytest.fixture()
def fake_llm():
    server = FakeLLMServer()
    try:
        yield server
    finally:
        server.stop()


def _openai_agent(fake_llm: FakeLLMServer, tmp_path: Any) -> AegisXAgent:
    config = AgentConfig(
        llm_provider=LLMProvider.CUSTOM,
        custom_base_url=fake_llm.base_url,
        custom_api_key="test-key",
        custom_model="fake-model",
        data_dir=str(tmp_path),
        rag_enabled=False,
        web_search_enabled=False,
        max_iterations=4,
    )
    return AegisXAgent(config)


def test_agent_completes_a_tool_round_trip(fake_llm: FakeLLMServer, tmp_path) -> None:
    fake_llm.script(
        openai_tool_call_response("call_1", "calculator", '{"expression": "2+2"}'),
        openai_text_response("The answer is 4."),
    )
    agent = _openai_agent(fake_llm, tmp_path)

    answer = run(agent.chat("what is 2+2?"))

    assert answer == "The answer is 4."
    # Exactly two calls: a successful turn must not be re-sent on success.
    assert len(fake_llm.requests) == 2

    path, first_request = fake_llm.requests[0]
    assert path.endswith("/chat/completions")
    assert first_request["model"] == "fake-model"
    assert first_request["messages"][0]["role"] == "system"
    assert "tools" in first_request
    assert any(
        tool["function"]["name"] == "calculator" for tool in first_request["tools"]
    )

    _, second_request = fake_llm.requests[1]
    assistant, tool_message = second_request["messages"][-2], second_request["messages"][-1]

    assert assistant["role"] == "assistant"
    assert assistant["tool_calls"][0]["id"] == "call_1"

    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call_1"
    assert tool_message["name"] == "calculator"
    assert tool_message["content"] == "2+2 = 4"


def test_agent_survives_a_rate_limit_retry(fake_llm: FakeLLMServer, tmp_path) -> None:
    fake_llm.script(too_many_requests(), openai_text_response("after retry"))
    agent = _openai_agent(fake_llm, tmp_path)

    answer = run(agent.chat("hello"))

    assert answer == "after retry"
    # One rate-limited attempt, then one success. Not five attempts.
    assert len(fake_llm.requests) == 2


def test_unknown_tool_over_http_does_not_break_the_run(
    fake_llm: FakeLLMServer, tmp_path
) -> None:
    fake_llm.script(
        openai_tool_call_response("call_missing", "no_such_tool", "{}"),
        # A failed tool triggers one recovery consultation first.
        openai_text_response(
            '{"analysis": "no such tool", "fix_suggestion": "none", '
            '"alternative_tool": null, "alternative_args": null}'
        ),
        openai_text_response("I could not do that."),
    )
    agent = _openai_agent(fake_llm, tmp_path)

    answer = run(agent.chat("use a tool that does not exist"))

    assert answer == "I could not do that."
    assert len(fake_llm.requests) == 3

    # The recovery turn is a plain consultation: no tools offered.
    assert "tools" not in fake_llm.requests[1][1]

    # Recovery gave no alternative, so the original error is handed back.
    tool_message = fake_llm.requests[2][1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call_missing"
    assert "not found" in tool_message["content"]


def test_streaming_round_trip_delivers_chunks(fake_llm: FakeLLMServer) -> None:
    fake_llm.script(sse_text(["Hel", "lo", "!"]))
    provider = CustomProvider(
        model="fake-model", api_key="test-key", base_url=fake_llm.base_url
    )

    chunks = run(_collect(provider.stream_chat([Message(role=Role.USER, content="hi")])))

    assert "".join(chunks) == "Hello!"
    assert fake_llm.requests[0][1]["stream"] is True


def test_chat_stream_runs_tools_then_streams(fake_llm: FakeLLMServer, tmp_path) -> None:
    fake_llm.script(
        openai_tool_call_response("call_s1", "calculator", '{"expression": "7*6"}'),
        sse_text(["42", " it is"]),
    )
    agent = _openai_agent(fake_llm, tmp_path)

    text = "".join(run(_collect(agent.chat_stream("what is 7*6?"))))

    assert "calculator" in text
    assert "42 it is" in text

    tool_message = fake_llm.requests[1][1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call_s1"
    assert tool_message["content"] == "7*6 = 42"


def test_anthropic_tool_round_trip(fake_llm: FakeLLMServer, tmp_path) -> None:
    fake_llm.script(
        anthropic_tool_use_response("toolu_1", "calculator", {"expression": "6*7"}),
        anthropic_text_response("It is 42."),
    )
    config = AgentConfig(
        llm_provider=LLMProvider.ANTHROPIC,
        anthropic_api_key="test-key",
        anthropic_model="claude-test",
        data_dir=str(tmp_path),
        rag_enabled=False,
        web_search_enabled=False,
        max_iterations=4,
    )
    agent = AegisXAgent(config)
    agent.llm = AnthropicProvider(
        model="claude-test", api_key="test-key", base_url=fake_llm.base_url
    )
    agent.agent_loop.llm = agent.llm

    answer = run(agent.chat("what is 6*7?"))

    assert answer == "It is 42."

    headers = {key.lower(): value for key, value in fake_llm.headers_seen[0].items()}
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["x-api-key"] == "test-key"

    _, first_request = fake_llm.requests[0]
    assert first_request["system"]
    assert first_request["messages"][-1]["role"] == "user"
    assert "input_schema" in first_request["tools"][0]

    _, second_request = fake_llm.requests[1]
    assistant = second_request["messages"][-2]
    tool_result = second_request["messages"][-1]

    assert {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "calculator",
        "input": {"expression": "6*7"},
    } in assistant["content"]

    assert tool_result["role"] == "user"
    assert tool_result["content"][0]["type"] == "tool_result"
    assert tool_result["content"][0]["tool_use_id"] == "toolu_1"
    assert tool_result["content"][0]["content"] == "6*7 = 42"

    assert second_request["system"]
