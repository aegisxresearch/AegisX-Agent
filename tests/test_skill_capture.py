"""End-to-end: a complex task is captured into the skill library."""

from __future__ import annotations

from typing import Any

from support import ScriptedLLM, run

from aegisx_agent.agent_loop import AgentTrace
from aegisx_agent.config import AgentConfig, LLMProvider
from aegisx_agent.core import AegisXAgent
from aegisx_agent.llm.base import LLMResponse, Message


def _agent(tmp_path: Any) -> AegisXAgent:
    config = AgentConfig(llm_provider=LLMProvider.OLLAMA, data_dir=str(tmp_path))
    return AegisXAgent(config)


def test_complex_task_is_captured_as_a_skill(tmp_path) -> None:
    agent = _agent(tmp_path)
    agent.llm = ScriptedLLM(
        [
            LLMResponse(
                content=(
                    '{"name": "captured", "description": "How we did it", '
                    '"steps": ["first", "second"]}'
                )
            )
        ]
    )

    run(
        agent._maybe_create_skill(
            "do the thing", "all done", AgentTrace(goal="g", total_tool_calls=5)
        )
    )

    skill = agent.skill_manager.get("captured")
    assert skill is not None
    assert skill.steps == ["first", "second"]
    assert skill.tags == ["auto-generated"]
    assert agent.last_skill_error == ""


def test_simple_task_is_not_captured(tmp_path) -> None:
    agent = _agent(tmp_path)
    agent.llm = ScriptedLLM([])  # must never be called

    run(
        agent._maybe_create_skill(
            "quick question", "answer", AgentTrace(goal="g", total_tool_calls=1)
        )
    )

    assert agent.skill_manager.list_skills() == []


def test_invalid_skill_json_is_reported_not_raised(tmp_path) -> None:
    agent = _agent(tmp_path)
    agent.llm = ScriptedLLM([LLMResponse(content="I would rather not")])

    run(
        agent._maybe_create_skill(
            "do the thing", "all done", AgentTrace(goal="g", total_tool_calls=5)
        )
    )

    assert agent.skill_manager.list_skills() == []
    assert agent.last_skill_error == "skill creator returned no JSON object"


def test_skill_capture_survives_llm_failure(tmp_path) -> None:
    class FailingLLM(ScriptedLLM):
        async def chat(
            self,
            messages: list[Message],
            tools: list[dict[str, Any]] | None = None,
            temperature: float = 0.7,
            max_tokens: int = 4096,
        ) -> LLMResponse:
            raise ConnectionError("provider down")

    agent = _agent(tmp_path)
    agent.llm = FailingLLM([])

    run(
        agent._maybe_create_skill(
            "do the thing", "all done", AgentTrace(goal="g", total_tool_calls=5)
        )
    )

    assert agent.last_skill_error == "ConnectionError: provider down"


def test_agent_registers_the_skill_tool(tmp_path) -> None:
    agent = _agent(tmp_path)

    assert agent.tools.get("skill") is not None
    assert "skill" in agent.list_tools()


def test_set_model_and_provider_rebuild_the_client(tmp_path) -> None:
    agent = _agent(tmp_path)
    previous = agent.llm

    agent.set_model("qwen2.5")
    assert agent.config.ollama_model == "qwen2.5"
    assert agent.llm is not previous
    assert agent.agent_loop.llm is agent.llm

    agent.set_provider("openai")
    assert agent.config.llm_provider is LLMProvider.OPENAI
    assert agent.get_provider_info()["provider"] == "openai"
