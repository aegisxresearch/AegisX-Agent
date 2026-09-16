"""Streaming calls the LLM exactly ONCE per turn.

The old flow spent a full non-streaming request probing for tool calls and
then re-sent the whole conversation as a streaming request — twice the tokens
and latency on every turn. These tests pin the new contract: tool calls are
read from the stream itself, so a toolless turn is ONE request and a tool
turn is two (the follow-up carries the tool result).

Everything runs against the fake HTTP server — real SSE frames, real socket.
"""

from __future__ import annotations

from typing import Any

import pytest
from fake_llm import FakeLLMServer, anthropic_sse_text, sse_text, sse_tool_call
from support import run

from aegisx_agent.config import AgentConfig, LLMProvider
from aegisx_agent.core import AegisXAgent


async def _collect(iterator: Any) -> list[str]:
    return [chunk async for chunk in iterator]


@pytest.fixture()
def fake_llm():
    server = FakeLLMServer()
    try:
        yield server
    finally:
        server.stop()


def _agent(fake_llm: FakeLLMServer, tmp_path: Any) -> AegisXAgent:
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


def test_a_toolless_streaming_turn_is_exactly_one_request(
    fake_llm: FakeLLMServer, tmp_path
) -> None:
    fake_llm.script(sse_text(["Hello", "!", " Bye."]))
    agent = _agent(fake_llm, tmp_path)

    text = "".join(run(_collect(agent.chat_stream("hi"))))

    assert text == "Hello! Bye."
    assert len(fake_llm.requests) == 1
    assert fake_llm.requests[0][1]["stream"] is True
    # The user's question must actually be in the request this time.
    last_message = fake_llm.requests[0][1]["messages"][-1]
    assert last_message["role"] == "user"
    assert last_message["content"] == "hi"


def test_a_tool_turn_is_request_plus_followup_and_streams_live(
    fake_llm: FakeLLMServer, tmp_path
) -> None:
    fake_llm.script(
        sse_tool_call("call_s1", "calculator", '{"expression": "7*6"}'),
        sse_text(["The answer is ", "42."]),
    )
    agent = _agent(fake_llm, tmp_path)

    chunks = run(_collect(agent.chat_stream("what is 7*6?")))
    text = "".join(chunks)

    # One request for the tool decision, one to deliver the result, and that
    # is all — no separate non-streaming probe.
    assert len(fake_llm.requests) == 2

    # The tool call was parsed from the stream and executed.
    assert "calculator: ✅" in text
    assert "The answer is 42." in text

    followup = fake_llm.requests[1][1]
    assert followup["stream"] is True
    tool_message = followup["messages"][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call_s1"


def test_streaming_finishes_a_turn_like_chat_does(
    fake_llm: FakeLLMServer, tmp_path
) -> None:
    """Memory, session store, and history must end in the same state as chat()."""
    fake_llm.script(sse_text(["All done."]))
    agent = _agent(fake_llm, tmp_path)

    text = "".join(run(_collect(agent.chat_stream("do it"))))

    assert text == "All done."

    # Conversation memory got both sides of the exchange.
    roles = [m.role.value for m in agent.conversation.messages]
    assert roles == ["user", "assistant"]
    assert agent.conversation.messages[-1].content == "All done."

    # Session store persisted both sides under the same session id.
    history = agent.session_store.get_session_history(agent.session_id)
    assert [entry["role"] for entry in history] == ["user", "assistant"]
    assert history[-1]["content"] == "All done."


def test_streamed_tool_turn_updates_memory_and_session_store(
    fake_llm: FakeLLMServer, tmp_path
) -> None:
    fake_llm.script(
        sse_tool_call("call_s2", "calculator", '{"expression": "6*7"}'),
        sse_text(["done"]),
    )
    agent = _agent(fake_llm, tmp_path)

    run(_collect(agent.chat_stream("multiply six by seven")))

    roles = [m.role.value for m in agent.conversation.messages]
    assert roles == ["user", "assistant"]
    assert agent.conversation.messages[-1].content == "done"

    history = agent.session_store.get_session_history(agent.session_id)
    assert [entry["role"] for entry in history] == ["user", "assistant"]
    assert history[-1]["content"] == "done"


def test_stream_chat_survives_a_provider_connection_error(
    fake_llm: FakeLLMServer, tmp_path
) -> None:
    """A dead stream raises a structured error, not a half-written turn."""
    agent = _agent(fake_llm, tmp_path)
    fake_llm.script()  # empty script -> server answers 500 "script exhausted"

    with pytest.raises(Exception):
        run(_collect(agent.chat_stream("hello")))

    # The turn was not completed, so nothing was persisted.
    assert agent.session_store.get_session_history(agent.session_id) == []


def test_anthropic_tool_calls_are_parsed_from_the_stream(
    fake_llm: FakeLLMServer, tmp_path
) -> None:
    """Anthropic SSE: content_block_start/input_json_delta -> executed tool."""
    frames = (
        'data: {"type":"message_start","message":{"usage":{"input_tokens":4}}}\n\n'
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"tool_use","id":"toolu_s1","name":"calculator"}}\n\n'
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"input_json_delta","partial_json":"{\\"expression\\": \\"6*7\\"}"}}\n\n'
        'data: {"type":"content_block_stop","index":0}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},'
        '"usage":{"output_tokens":3}}\n\n'
        'data: {"type":"message_stop"}\n\n'
    )
    fake_llm.script({"sse": frames}, anthropic_sse_text(["42 is the answer."]))
    agent = _agent(fake_llm, tmp_path)
    from aegisx_agent.llm.anthropic_provider import AnthropicProvider

    agent.llm = AnthropicProvider(
        model="claude-test", api_key="test-key", base_url=fake_llm.base_url
    )
    agent.agent_loop.llm = agent.llm

    text = "".join(run(_collect(agent.chat_stream("what is 6*7?"))))

    assert len(fake_llm.requests) == 2
    assert "calculator: ✅" in text
    assert "42 is the answer." in text

    # On the Anthropic wire, tool results travel inside a user turn as
    # tool_result blocks (see the non-streaming round-trip test).
    tool_result_turn = fake_llm.requests[1][1]["messages"][-1]
    assert tool_result_turn["role"] == "user"
    assert tool_result_turn["content"][0]["type"] == "tool_result"
    assert tool_result_turn["content"][0]["tool_use_id"] == "toolu_s1"
    assert tool_result_turn["content"][0]["content"] == "6*7 = 42"


def test_stream_announces_each_tool_call_before_it_runs(
    fake_llm: FakeLLMServer, tmp_path
) -> None:
    """The loop fires on_tool_start with the raw wire arguments."""
    fake_llm.script(
        sse_tool_call("call_s3", "file_ops", '{"action": "write", "path": "login.html"}'),
        sse_text(["done"]),
    )
    agent = _agent(fake_llm, tmp_path)

    starts: list[tuple[str, Any]] = []
    original_run = agent.agent_loop._run

    async def spy_run(*args: Any, **kwargs: Any) -> Any:
        callback = kwargs.get("on_tool_start")

        def on_tool_start(name: str, arguments: Any) -> None:
            starts.append((name, arguments))
            if callback is not None:
                callback(name, arguments)

        kwargs["on_tool_start"] = on_tool_start
        return await original_run(*args, **kwargs)

    agent.agent_loop._run = spy_run  # type: ignore[method-assign]

    chunks = run(_collect(agent.chat_stream("make login page")))
    text = "".join(chunks)

    # The hook fired with the tool's raw wire arguments, before execution.
    assert starts == [("file_ops", '{"action": "write", "path": "login.html"}')]
    # The stream carried the start line (delegating to the agent's formatter)
    # and the completion marker after execution.
    assert "file_ops → login.html" in text
    assert "file_ops: ✅" in text


def test_tool_status_line_surfaces_the_skim_argument() -> None:
    """The status line names the tool and the argument a human cares about."""
    from aegisx_agent.core.agent import _tool_status_line

    line = _tool_status_line("file_ops", '{"action": "write", "path": "login.html"}')
    assert line == "📁 file_ops → login.html"

    # Malformed JSON degrades to a plain tool-name line.
    assert _tool_status_line("file_ops", "not json") == "📁 file_ops"
    # Unknown tools get the generic glyph.
    assert _tool_status_line("mystery", None) == "🔧 mystery"


def test_status_lines_do_not_arrive_for_toolless_turns(
    fake_llm: FakeLLMServer, tmp_path
) -> None:
    fake_llm.script(sse_text(["Just chatting."]))
    agent = _agent(fake_llm, tmp_path)

    chunks = run(_collect(agent.chat_stream("hi again")))

    assert "".join(chunks) == "Just chatting."
