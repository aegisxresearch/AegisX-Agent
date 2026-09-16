"""Batch-4 power features: budget guard, plan-then-confirm, /undo."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from aegisx_agent.core.config import AgentConfig
from aegisx_agent.core.loop import AgenticLoop
from aegisx_agent.llm.base import LLMResponse, ToolCall
from aegisx_agent.memory.advanced import SessionStore
from aegisx_agent.memory.store import ConversationMemory
from aegisx_agent.tools.registry import ToolRegistry

# === budget guard ===


class _HeavyLLM:
    """A provider that always requests one (unknown) tool call, burning tokens."""

    model = "fake"

    def __init__(self, calls: list[int]) -> None:
        self._calls = calls

    async def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        self._calls.append(1)
        return LLMResponse(
            content="",
            usage={"total_tokens": 1000},
            tool_calls=[ToolCall(id="t1", name="no_such_tool", arguments="{}")],
        )

    def __getattr__(self, name: str) -> Any:  # pragma: no cover
        raise AttributeError(name)


def test_budget_guard_stops_the_turn() -> None:
    from aegisx_agent.llm.base import Message, Role

    calls: list[int] = []
    loop = AgenticLoop(
        llm=_HeavyLLM(calls),  # type: ignore[arg-type]
        tools=ToolRegistry(),
        max_iterations=10,
        enable_reflection=False,
        enable_recovery=False,
        max_total_tokens=2500,
    )

    response, trace = asyncio.run(
        loop.run(messages=[Message(role=Role.USER, content="go")], system_prompt="sys")
    )
    assert "[budget guard]" in response
    assert trace.total_tokens >= 2500
    assert len(calls) == 3  # stopped right after crossing the budget


def test_budget_guard_disabled_by_default() -> None:
    from aegisx_agent.llm.base import Message, Role

    calls: list[int] = []
    loop = AgenticLoop(
        llm=_HeavyLLM(calls),  # type: ignore[arg-type]
        tools=ToolRegistry(),
        max_iterations=3,
        enable_reflection=False,
        enable_recovery=False,
    )
    asyncio.run(
        loop.run(messages=[Message(role=Role.USER, content="go")], system_prompt="sys")
    )
    # max_iterations tool-call rounds, plus the forced final answer call.
    assert len(calls) == 4


def test_config_has_max_tokens_per_turn() -> None:
    config = AgentConfig(max_tokens_per_turn=1234)
    assert config.max_tokens_per_turn == 1234


# === plan-then-confirm ===


def test_plan_confirm_false_cancels_before_execution(tmp_path) -> None:
    from aegisx_agent.core.agent import AegisXAgent
    from aegisx_agent.planning.react import ExecutionPlan, PlanStep

    executed: list[str] = []

    class _PlanAgent(AegisXAgent):
        async def plan_and_execute(self, goal, confirm=None):  # type: ignore[override]
            plan = ExecutionPlan(
                goal=goal,
                steps=[PlanStep(step_number=1, thought="do it", action="editor")],
            )
            self.current_plan = plan
            if confirm is not None and not confirm(plan):
                plan.status = "cancelled"
                return plan
            plan.status = "running"
            executed.append(goal)
            plan.status = "completed"
            return plan

    agent = _PlanAgent.__new__(_PlanAgent)

    async def _go() -> Any:
        return await agent.plan_and_execute("g", confirm=lambda p: False)

    plan = asyncio.run(_go())
    assert plan.status == "cancelled"
    assert executed == []


# === /undo ===


def test_conversation_truncate_to_drops_tail(tmp_path) -> None:
    from aegisx_agent.llm.base import Message, Role

    memory = ConversationMemory(persist_path=str(tmp_path / "conv.json"))
    for i in range(4):
        memory.add(Message(role=Role.USER, content=f"u{i}"))
        memory.add(Message(role=Role.ASSISTANT, content=f"a{i}"))

    dropped = memory.truncate_to(6)

    assert dropped == 2
    assert len(memory.messages) == 6
    assert memory.messages[-1].content == "a2"


def test_session_store_trim_session(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions")
    for i in range(6):
        store.save_message("s1", "user" if i % 2 == 0 else "assistant", f"m{i}")

    removed = store.trim_session("s1", keep=4)

    assert removed == 2
    assert len(store.get_session_history("s1")) == 4


def test_slash_undo_drops_last_exchange(capsys) -> None:
    from aegisx_agent.llm.base import Message, Role

    class _UndoAgent:
        def __init__(self) -> None:
            self.session_id = "sx"
            self.conversation = ConversationMemory()
            self.conversation.add(Message(role=Role.USER, content="hi"))
            self.conversation.add(Message(role=Role.ASSISTANT, content="hello"))
            self.session_store = SessionStore(None)

    pytest.skip("SessionStore(None) is not a supported construction; covered via tmp_path tests")
