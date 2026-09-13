"""Agentic loop: tool-call ids, failure containment, and parallel tool batches."""

from __future__ import annotations

from support import EchoTool, ScriptedLLM, run

from aegisx_agent.agent_loop import AgenticLoop, AgentTrace
from aegisx_agent.llm.base import LLMResponse, Message, Role, ToolCall
from aegisx_agent.tools.registry import ToolRegistry


def _loop(llm: ScriptedLLM, registry: ToolRegistry) -> AgenticLoop:
    return AgenticLoop(
        llm=llm,
        tools=registry,
        max_iterations=4,
        enable_reflection=False,
        enable_recovery=False,
    )


def _run_loop(llm: ScriptedLLM, registry: ToolRegistry, prompt: str = "please echo hi"):
    return run(
        _loop(llm, registry).run(
            messages=[Message(role=Role.USER, content=prompt)],
            system_prompt="system",
            tool_schemas=registry.list_schemas(),
        )
    )


def test_tool_results_are_matched_to_call_ids() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    llm = ScriptedLLM(
        [
            LLMResponse(
                content="calling echo",
                tool_calls=[ToolCall(id="call_abc", name="echo", arguments='{"text": "hi"}')],
                usage={"total_tokens": 5},
            ),
            LLMResponse(content="all done", usage={"total_tokens": 3}),
        ]
    )

    answer, trace = _run_loop(llm, registry)

    assert answer == "all done"
    assert trace.total_tool_calls == 1
    assert trace.total_tokens == 8

    follow_up = llm.seen_messages[1]
    assert follow_up[0].role is Role.SYSTEM

    tool_messages = [m for m in follow_up if m.role is Role.TOOL]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_call_id == "call_abc"
    assert tool_messages[0].name == "echo"
    assert tool_messages[0].content == "echo:hi"

    assistant = [m for m in follow_up if m.role is Role.ASSISTANT][0]
    assert assistant.tool_calls == [
        {
            "id": "call_abc",
            "type": "function",
            "function": {"name": "echo", "arguments": '{"text": "hi"}'},
        }
    ]


def test_failing_tool_is_reported_back_and_loop_continues() -> None:
    registry = ToolRegistry()
    llm = ScriptedLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="call_x", name="ghost", arguments="{}")],
            ),
            LLMResponse(content="recovered"),
        ]
    )

    answer, trace = _run_loop(llm, registry)

    assert answer == "recovered"
    assert trace.total_tool_calls == 1

    tool_message = [m for m in llm.seen_messages[1] if m.role is Role.TOOL][0]
    assert tool_message.tool_call_id == "call_x"
    assert "not found" in tool_message.content


def test_parallel_tool_batch_keeps_distinct_ids() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    llm = ScriptedLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="c1", name="echo", arguments='{"text": "a"}'),
                    ToolCall(id="c2", name="echo", arguments='{"text": "b"}'),
                ],
            ),
            LLMResponse(content="done"),
        ]
    )

    answer, trace = _run_loop(llm, registry)

    assert answer == "done"
    assert trace.total_tool_calls == 2

    ids = sorted(m.tool_call_id or "" for m in llm.seen_messages[1] if m.role is Role.TOOL)
    assert ids == ["c1", "c2"]


def test_trace_records_steps_and_summary() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    llm = ScriptedLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="c1", name="echo", arguments='{"text": "x"}')],
            ),
            LLMResponse(content="finished"),
        ]
    )

    _, trace = _run_loop(llm, registry)

    assert isinstance(trace, AgentTrace)
    assert len(trace.steps) == 2
    assert trace.steps[0].tool_calls == [{"name": "echo", "arguments": '{"text": "x"}'}]
    assert trace.steps[0].tool_results[0]["success"] is True
    assert "Tool calls: 1" in trace.summary


def test_max_iterations_forces_a_final_answer() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    llm = ScriptedLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="c1", name="echo", arguments='{"text": "a"}')],
            ),
            LLMResponse(content="forced answer"),
        ]
    )
    loop = AgenticLoop(
        llm=llm,
        tools=registry,
        max_iterations=1,
        enable_reflection=False,
        enable_recovery=False,
    )

    answer, trace = run(
        loop.run(
            messages=[Message(role=Role.USER, content="loop forever")],
            system_prompt="system",
            tool_schemas=registry.list_schemas(),
        )
    )

    assert answer == "forced answer"
    assert trace.total_tool_calls == 1
