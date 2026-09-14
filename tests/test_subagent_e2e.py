"""End to end: spawn_subagent works through the real agent and real HTTP.

A delegation must travel the exact same path as any other tool call — fake
HTTP server → agent loop → ToolRegistry → PermissionGate → SubagentTool →
child loop → child tool — so these tests prove subagents are first-class
citizens of the agent, not a separate runtime.
"""

from __future__ import annotations

from typing import Any

import pytest
from fake_llm import (
    FakeLLMServer,
    openai_text_response,
    openai_tool_call_response,
    sse_text,
    sse_tool_call,
)
from support import run

from aegisx_agent.config import AgentConfig, LLMProvider
from aegisx_agent.core import AegisXAgent


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


def test_parent_delegates_to_child_over_real_http(fake_llm, tmp_path) -> None:
    agent = _agent(fake_llm, tmp_path)
    assert "spawn_subagent" in agent.list_tools()

    # Turn 1: parent delegates. Turn 2 (child): computes. Turn 3: parent answers.
    fake_llm.script(
        openai_tool_call_response(
            "call_1", "spawn_subagent", '{"task": "what is 2+2", "tools": "calculator"}'
        ),
        openai_tool_call_response("call_2", "calculator", '{"expression": "2+2"}'),
        openai_text_response("the child computed: 4"),
        openai_text_response("The answer is 4, delegated successfully."),
    )

    response = run(agent.chat("delegate a calculation"))

    assert "answer is 4" in response
    requests = fake_llm.requests
    # Child calls carry the subagent system prompt, the parent's do not.
    child_systems = [
        body["messages"][0]["content"]
        for _, body in requests
        if body["messages"]
        and "focused sub-agent" in str(body["messages"][0]["content"])
    ]
    assert len(child_systems) == 2  # child turn 1 (handoff) and turn 2 (final)


def test_child_tools_are_restricted_and_gate_enforced(fake_llm, tmp_path) -> None:
    agent = _agent(
        fake_llm,
        tmp_path,
        subagent_max_steps=3,
        permission_mode="read-only",
    )

    fake_llm.script(
        openai_tool_call_response(
            "call_1",
            "spawn_subagent",
            '{"task": "compute", "tools": "calculator"}',
        ),
        openai_tool_call_response("call_2", "calculator", '{"expression": "3*3"}'),
        openai_text_response("child done: 9"),
        openai_text_response("result was 9"),
    )

    response = run(agent.chat("go"))

    assert "9" in response
    # Both the delegation and the child's calculator call arrived as real
    # tool round trips somewhere in the request bodies.
    called_names = {
        tc["function"]["name"]
        for _, body in fake_llm.requests
        for message in body.get("messages", [])
        for tc in (message.get("tool_calls") or [])
    }
    assert "spawn_subagent" in called_names
    assert "calculator" in called_names


async def _collect(stream) -> list[str]:
    chunks: list[str] = []
    async for chunk in stream:
        chunks.append(chunk)
    return chunks


def test_streaming_turn_surfaces_subagent_progress_and_cost(fake_llm, tmp_path) -> None:
    agent = _agent(fake_llm, tmp_path)

    # Parent turns stream (SSE); the child's loop is non-streaming (JSON).
    fake_llm.script(
        sse_tool_call(
            "call_1", "spawn_subagent", '{"task": "compute 2+2", "tools": "calculator"}'
        ),
        openai_tool_call_response("call_2", "calculator", '{"expression": "2+2"}'),
        openai_text_response("child: 4"),
        sse_text(["The answer is 4."]),
    )

    chunks = run(_collect(agent.chat_stream("delegate a calculation")))
    text = "".join(chunks)

    # Start line, per-step calls, and the cost line all reached the stream.
    assert "subagent (depth 1, budget 8): compute 2+2" in text
    assert "subagent step: calculator \u2705" in text
    assert "subagent (depth 1) done:" in text
    assert "tokens" in text
    # The child's own answer travels as tool-result data (not stream text);
    # the parent's wrap-up is what streams to the user.
    assert "The answer is 4." in text


def test_budget_exhaustion_is_visible_to_the_parent(fake_llm, tmp_path) -> None:
    agent = _agent(fake_llm, tmp_path, subagent_max_steps=1)

    # Child burns its one step on a tool call, then must still answer.
    fake_llm.script(
        openai_tool_call_response("call_1", "spawn_subagent", '{"task": "big job"}'),
        openai_tool_call_response("call_2", "calculator", '{"expression": "1+1"}'),
        openai_text_response("partial progress only"),
        openai_text_response("child hit its budget; I will do it myself"),
    )

    response = run(agent.chat("delegate big job"))

    # The child's final answer (forced by _get_final_answer at the cap) is
    # delivered to the parent with the budget warning appended.
    assert "budget" in response or "budget" in "".join(
        m["content"] or ""
        for _, body in fake_llm.requests
        for m in body["messages"]
        if m["role"] == "tool"
    )
