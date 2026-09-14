"""SubagentTool: restriction, budget, depth cap, timeout, and gate sharing."""

from __future__ import annotations

import asyncio
from typing import Any

from support import EchoTool, ScriptedLLM, SpyTool, run

from aegisx_agent.llm.base import LLMProvider, LLMResponse, Message, Role, ToolCall
from aegisx_agent.security.permissions import PermissionGate, PermissionMode
from aegisx_agent.tools.calculator import CalculatorTool
from aegisx_agent.tools.registry import ToolRegistry
from aegisx_agent.tools.subagent import (
    DEFAULT_SUBAGENT_TOOLS,
    SubagentTool,
)


class SlowLLM(LLMProvider):
    """LLM whose chat never returns inside a test timeout."""

    def __init__(self) -> None:
        super().__init__(model="slow")

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        await asyncio.sleep(30)
        raise AssertionError("chat should have been cancelled by the timeout")

    async def stream_chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> Any:
        await self.chat(messages)
        yield LLMResponse()  # pragma: no cover - unreachable

    async def run_streaming(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        on_chunk: Any = None,
    ) -> LLMResponse:
        return await self.chat(messages)


def _text(text: str) -> LLMResponse:
    return LLMResponse(content=text, usage={"total_tokens": 4})


def _tool_call(call_id: str, name: str, arguments: str) -> LLMResponse:
    return LLMResponse(
        content="calling",
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
        usage={"total_tokens": 5},
    )


def _tool(
    llm: ScriptedLLM,
    registry: ToolRegistry,
    gate: PermissionGate | None = None,
    **overrides: Any,
) -> SubagentTool:
    defaults: dict[str, Any] = {
        "llm_factory": lambda: llm,
        "parent_registry": registry,
        "gate": gate,
        "max_steps": 4,
        "max_depth": 1,
        "timeout": 10.0,
    }
    defaults.update(overrides)
    return SubagentTool(**defaults)


def _spawn(task: str, tools: str | None = None) -> str:
    arguments = f'{{"task": "{task}"}}' if tools is None else (
        f'{{"task": "{task}", "tools": "{tools}"}}'
    )
    return arguments


def test_subagent_round_trip_returns_child_answer_and_metadata() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    llm = ScriptedLLM(
        [
            _tool_call("c1", "echo", '{"text": "hi"}'),
            _text("child final answer"),
        ]
    )
    tool = _tool(llm, registry)

    result = run(tool.execute(task="say hi", tools="echo"))

    assert result.is_success
    assert result.output == "child final answer"
    assert result.metadata["tool_calls"] == 1
    assert result.metadata["steps"] == 2
    assert result.metadata["total_tokens"] == 9
    assert result.metadata["tools_given"] == ["echo"]
    assert result.metadata["budget_exhausted"] is False
    assert result.metadata["depth"] == 1

    # The child saw its own system prompt with its budget and tool list.
    child_system = llm.seen_messages[0][0]
    assert child_system.role is Role.SYSTEM
    assert "step budget of 4" in child_system.content
    assert "echo" in child_system.content


def test_subagent_defaults_are_intersected_with_the_parent_registry() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())  # exists but is not a default
    llm = ScriptedLLM([_text("done")])
    tool = _tool(llm, registry)

    result = run(tool.execute(task="just think"))

    assert result.is_success
    # Defaults that do not exist in the parent are dropped, never improvised.
    assert result.metadata["tools_given"] == []


def test_default_tools_present_in_the_parent_are_granted() -> None:
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    llm = ScriptedLLM([_text("42")])
    tool = _tool(llm, registry)

    result = run(tool.execute(task="compute 2+2"))

    assert result.is_success
    assert result.metadata["tools_given"] == ["calculator"]


def test_unknown_requested_tools_are_filtered_and_reported() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    llm = ScriptedLLM([_text("done")])
    tool = _tool(llm, registry)

    result = run(tool.execute(task="t", tools="echo, ghost_tool, spawn_subagent"))

    assert result.is_success
    # Only real parent tools pass; spawn_subagent itself is not granted here.
    assert result.metadata["tools_given"] == ["echo"]
    assert "ghost_tool" in result.output
    assert "requested tools not found" in result.output


def test_step_budget_stops_the_child_and_is_reported() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    llm = ScriptedLLM(
        [
            _tool_call("1", "echo", '{"text": "a"}'),
            _tool_call("2", "echo", '{"text": "b"}'),
            # Never gets to run: the budget is 2.
            _tool_call("3", "echo", '{"text": "c"}'),
            _tool_call("4", "echo", '{"text": "d"}'),
        ]
    )
    tool = _tool(llm, registry, max_steps=2)

    result = run(tool.execute(task="loop forever"))

    assert result.is_success
    assert result.metadata["tool_calls"] == 2
    assert result.metadata["budget_exhausted"] is True
    assert "stopped at its 2-step budget" in result.output


def test_timeout_returns_a_timeout_result_without_raising() -> None:
    registry = ToolRegistry()
    llm = SlowLLM()
    tool = _tool(llm, registry, timeout=0.05)

    result = run(tool.execute(task="hang"))

    assert result.status.value == "timeout"
    assert "timed out after 0.05s" in result.error
    assert result.metadata["tools_given"] == list(DEFAULT_SUBAGENT_TOOLS)


def test_child_calls_share_the_parent_permission_gate() -> None:
    registry = ToolRegistry()
    spy = SpyTool()
    registry.register(spy)
    gate = PermissionGate(mode=PermissionMode.READ_ONLY)
    llm = ScriptedLLM(
        [
            _tool_call("c1", "spy", '{"action": "delete"}'),
            _text("I saw the refusal"),
        ]
    )
    tool = _tool(llm, registry, gate=gate)

    result = run(tool.execute(task="delete things", tools="spy"))

    assert result.is_success
    # The child's dangerous call never executed — the shared gate refused it.
    assert spy.calls == []
    tool_message = [m for m in llm.seen_messages[1] if m.role is Role.TOOL][0]
    assert "Permission denied" in tool_message.content


def test_depth_cap_removes_spawn_from_the_deepest_generation() -> None:
    registry = ToolRegistry()
    llm = ScriptedLLM([_text("leaf")])

    capped = _tool(llm, registry, max_depth=2, depth=2)
    nested = _tool(llm, registry, max_depth=2, depth=1)

    child_registry = nested._child_registry(["calculator"])
    grandchild_registry = capped._child_registry([])

    assert child_registry.get("spawn_subagent") is not None
    assert grandchild_registry.get("spawn_subagent") is None
    assert llm.seen_messages == []  # construction never calls the LLM


def test_nested_child_registry_only_narrows() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    llm = ScriptedLLM([_text("leaf")])
    tool = _tool(llm, registry, max_depth=3, depth=1)

    child_registry = tool._child_registry(["echo"])

    assert child_registry.get("echo") is not None
    nested = child_registry.get("spawn_subagent")
    assert isinstance(nested, SubagentTool)
    assert nested.depth == 2
    # The child's spawner resolves tools against the child registry: the
    # default read-only builtins are not present there, so they are dropped.
    names, note = nested._resolve_tools(None)
    assert names == []
    assert note == ""


def test_progress_hook_receives_start_steps_and_cost_events() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    llm = ScriptedLLM(
        [
            _tool_call("c1", "echo", '{"text": "hi"}'),
            _text("child final answer"),
        ]
    )
    events: list[str] = []
    tool = _tool(llm, registry, on_progress=events.append)

    result = run(tool.execute(task="say hi", tools="echo"))

    assert result.is_success
    assert len(events) == 3
    assert events[0].startswith("⏵ subagent (depth 1, budget 4): say hi")
    assert events[1] == "  ⏳ subagent step: echo ✅"
    assert events[2] == "⏵ subagent (depth 1) done: 1 tool calls, 9 tokens, 0.0s"


def test_progress_hook_reports_failures_timeouts_and_swallows_observer_errors() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())

    # A failing child call must surface as ❌.
    failing = ScriptedLLM(
        [
            _tool_call("c1", "ghost", "{}"),
            _text("recovered anyway"),
        ]
    )
    failing_events: list[str] = []
    tool = _tool(failing, registry, on_progress=failing_events.append)
    run(tool.execute(task="t", tools="echo"))
    assert "  ⏳ subagent step: ghost ❌" in failing_events

    # A broken observer must never break the run.
    def _exploding(event: str) -> None:
        raise RuntimeError("observer kaboom")

    llm = ScriptedLLM([_text("still fine")])
    tool = _tool(llm, registry, on_progress=_exploding)
    result = run(tool.execute(task="t"))
    assert result.is_success

    # A timeout emits its own event.
    timeout_events: list[str] = []
    slow = _tool(SlowLLM(), registry, on_progress=timeout_events.append, timeout=0.05)
    run(slow.execute(task="hang"))
    assert timeout_events[-1].startswith("  ⏹ subagent (depth 1) timed out after 0.05s")


def test_nested_spawner_inherits_the_progress_hook() -> None:
    registry = ToolRegistry()
    llm = ScriptedLLM([_text("leaf")])
    events: list[str] = []
    tool = _tool(llm, registry, max_depth=2, on_progress=events.append)

    child_registry = tool._child_registry([])
    nested = child_registry.get("spawn_subagent")
    assert isinstance(nested, SubagentTool)
    assert nested._on_progress is tool._on_progress

    # Events from a nested run carry the deeper depth tag.
    run(nested.execute(task="grandchild job"))
    assert any("depth 2" in event for event in events)


def test_no_hook_means_no_events_and_plain_runs_stay_quiet() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    llm = ScriptedLLM([_text("quiet")])
    tool = _tool(llm, registry)

    result = run(tool.execute(task="t", tools="echo"))
    assert result.is_success  # would raise if _emit touched a missing hook


def test_spawn_tool_schema_and_constructor_validation() -> None:
    registry = ToolRegistry()
    tool = _tool(ScriptedLLM([_text("x")]), registry)

    schema = tool.to_schema()["function"]
    assert schema["name"] == "spawn_subagent"
    assert schema["parameters"]["required"] == ["task"]

    result = run(tool.execute())
    assert not result.is_success
    assert "non-empty 'task'" in result.error

    for bad in ({"max_steps": 0}, {"max_depth": 0}, {"timeout": 0}):
        try:
            _tool(ScriptedLLM([_text("x")]), registry, **bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad}")


def test_system_prompt_mentions_budget_and_tools() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    llm = ScriptedLLM([_text("done")])
    tool = _tool(llm, registry, max_steps=6)

    run(tool.execute(task="t", tools="echo"))

    system = llm.seen_messages[0][0].content
    assert "step budget of 6" in system
    assert "Available tools: echo" in system
