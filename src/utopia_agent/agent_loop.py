"""Enhanced agentic loop — the core reasoning engine.

Features:
- Self-reflection: agent evaluates its own responses
- Error recovery: retries failed tools with different strategies
- Parallel tool execution: runs independent tools concurrently
- Autonomous chaining: decides when to continue reasoning
- Step validation: checks if each step moves toward the goal
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from utopia_agent.llm.base import LLMProvider, LLMResponse, Message, Role, ToolCall
from utopia_agent.tools.base import ToolResult, ToolStatus
from utopia_agent.tools.registry import ToolRegistry


class StepOutcome(str, Enum):
    """Outcome of an agent reasoning step."""

    COMPLETED = "completed"  # Task is done
    NEEDS_TOOLS = "needs_tools"  # Need to call tools
    NEEDS_REFLECTION = "needs_reflection"  # Need to self-reflect
    NEEDS_RETRY = "needs_retry"  # Tool failed, retry
    FAILED = "failed"  # Cannot complete


@dataclass
class AgentStep:
    """Record of a single agent step."""

    iteration: int
    thought: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    reflection: str | None = None
    outcome: StepOutcome = StepOutcome.NEEDS_TOOLS
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "thought": self.thought,
            "tool_calls": self.tool_calls,
            "tool_results": self.tool_results,
            "reflection": self.reflection,
            "outcome": self.outcome.value,
        }


@dataclass
class AgentTrace:
    """Full trace of an agent execution."""

    goal: str
    steps: list[AgentStep] = field(default_factory=list)
    final_response: str = ""
    total_tokens: int = 0
    total_tool_calls: int = 0
    duration_seconds: float = 0.0

    @property
    def summary(self) -> str:
        lines = [
            f"Goal: {self.goal}",
            f"Steps: {len(self.steps)}",
            f"Tool calls: {self.total_tool_calls}",
            f"Duration: {self.duration_seconds:.1f}s",
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "steps": [s.to_dict() for s in self.steps],
            "final_response": self.final_response,
            "total_tokens": self.total_tokens,
            "total_tool_calls": self.total_tool_calls,
            "duration_seconds": self.duration_seconds,
        }


REFLECTION_PROMPT = """You are reflecting on your previous response to a user query.

User query: {query}

Your previous response:
{response}

Tool results you received:
{tool_results}

Evaluate your response:
1. Is it complete and accurate?
2. Did you use the right tools?
3. Is there anything you missed?
4. Should you continue working or is this good enough?

Respond in JSON:
{{
    "assessment": "complete" or "needs_more_work",
    "issues": ["list of issues if any"],
    "next_action": "what to do next, or null if complete"
}}"""

RECOVERY_PROMPT = """A tool call failed. Analyze the error and suggest a fix.

Failed tool: {tool_name}
Arguments: {arguments}
Error: {error}

Previous context: {context}

Suggest a corrected approach. Respond in JSON:
{{
    "analysis": "what went wrong",
    "fix_suggestion": "how to fix it",
    "alternative_tool": "alternative tool to use, or null",
    "alternative_args": {{}} or null
}}"""


class AgenticLoop:
    """Enhanced reasoning loop with self-reflection and error recovery."""

    def __init__(
        self,
        llm: LLMProvider,
        tools: ToolRegistry,
        max_iterations: int = 15,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        enable_reflection: bool = True,
        enable_recovery: bool = True,
        parallel_tools: bool = True,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.max_iterations = max_iterations
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.enable_reflection = enable_reflection
        self.enable_recovery = enable_recovery
        self.parallel_tools = parallel_tools

    async def run(
        self,
        messages: list[Message],
        system_prompt: str,
        tool_schemas: list[dict[str, Any]] | None = None,
    ) -> tuple[str, AgentTrace]:
        """Run the agentic loop. Returns (final_response, trace)."""
        trace = AgentTrace(goal=messages[-1].content if messages else "")
        start_time = time.time()

        # Build working messages
        working_messages = [Message(role=Role.SYSTEM, content=system_prompt)] + list(messages)
        tool_results_context: list[str] = []

        for iteration in range(self.max_iterations):
            step = AgentStep(iteration=iteration + 1, thought="")

            # Get LLM response
            response = await self.llm.chat(
                messages=working_messages,
                tools=tool_schemas,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )

            trace.total_tokens += response.usage.get("total_tokens", 0)

            # No tool calls = we're done
            if not response.has_tool_calls:
                final = response.content or ""
                step.thought = final[:500]
                step.outcome = StepOutcome.COMPLETED
                trace.steps.append(step)
                trace.final_response = final
                trace.duration_seconds = time.time() - start_time
                return final, trace

            # Record tool calls
            step.thought = response.content or "(calling tools)"
            step.tool_calls = [
                {"name": tc.name, "arguments": tc.arguments} for tc in response.tool_calls
            ]

            # Execute tools (parallel or sequential)
            tool_results = await self._execute_tools(
                response.tool_calls, tool_results_context
            )
            step.tool_results = [
                {"name": r["name"], "success": r["success"], "output": r["output"][:200]}
                for r in tool_results
            ]
            trace.total_tool_calls += len(response.tool_calls)

            # Add to working messages
            tool_calls_dicts = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in response.tool_calls
            ]
            working_messages.append(
                Message(
                    role=Role.ASSISTANT,
                    content=response.content or "",
                    tool_calls=tool_calls_dicts,
                )
            )

            for r in tool_results:
                working_messages.append(
                    Message(
                        role=Role.TOOL,
                        content=r["output"],
                        tool_call_id=r["name"],
                        name=r["name"],
                    )
                )

            # Self-reflection every 3 steps
            if (
                self.enable_reflection
                and iteration > 0
                and iteration % 3 == 0
            ):
                reflection = await self._self_reflect(
                    trace.goal, working_messages, tool_results_context
                )
                step.reflection = reflection

                if reflection.get("assessment") == "complete":
                    # Agent thinks it's done
                    final = await self._get_final_answer(working_messages)
                    step.outcome = StepOutcome.COMPLETED
                    trace.steps.append(step)
                    trace.final_response = final
                    trace.duration_seconds = time.time() - start_time
                    return final, trace

            step.outcome = StepOutcome.NEEDS_TOOLS
            trace.steps.append(step)

        # Max iterations — force final answer
        final = await self._get_final_answer(working_messages)
        trace.final_response = final
        trace.duration_seconds = time.time() - start_time
        return final, trace

    async def _execute_tools(
        self,
        tool_calls: list[ToolCall],
        context: list[str],
    ) -> list[dict[str, Any]]:
        """Execute tool calls, optionally in parallel."""
        if self.parallel_tools and len(tool_calls) > 1:
            return await self._execute_parallel(tool_calls, context)
        return await self._execute_sequential(tool_calls, context)

    async def _execute_sequential(
        self, tool_calls: list[ToolCall], context: list[str]
    ) -> list[dict[str, Any]]:
        """Execute tools one by one."""
        results = []
        for tc in tool_calls:
            result = await self._execute_single_tool(tc, context)
            results.append(result)
        return results

    async def _execute_parallel(
        self, tool_calls: list[ToolCall], context: list[str]
    ) -> list[dict[str, Any]]:
        """Execute tools concurrently."""
        tasks = [self._execute_single_tool(tc, context) for tc in tool_calls]
        return await asyncio.gather(*tasks)

    async def _execute_single_tool(
        self, tc: ToolCall, context: list[str]
    ) -> dict[str, Any]:
        """Execute a single tool with error recovery."""
        result = await self.tools.execute(tc.name, tc.arguments)

        # If failed and recovery enabled, try to fix
        if not result.is_success and self.enable_recovery:
            recovered = await self._recover_from_error(tc, result, context)
            if recovered:
                return recovered

        output = result.to_llm_message()
        context.append(f"[{tc.name}] {output[:500]}")

        return {
            "name": tc.name,
            "success": result.is_success,
            "output": output,
            "status": result.status.value,
        }

    async def _self_reflect(
        self, goal: str, messages: list[Message], context: list[str]
    ) -> dict[str, Any]:
        """Have the agent reflect on its progress."""
        # Get last assistant message
        last_response = ""
        last_tools = ""
        for m in reversed(messages):
            if m.role == Role.ASSISTANT and m.content:
                last_response = m.content
                break
            if m.role == Role.TOOL:
                last_tools = f"{m.content[:300]}\n{last_tools}"

        prompt = REFLECTION_PROMPT.format(
            query=goal,
            response=last_response[:2000],
            tool_results=last_tools[:1000] or "No tool results yet",
        )

        try:
            response = await self.llm.chat(
                messages=[
                    Message(role=Role.SYSTEM, content="You are a self-reflection module. Respond with JSON only."),
                    Message(role=Role.USER, content=prompt),
                ],
                temperature=0.3,
                max_tokens=500,
            )

            json_start = (response.content or "").find("{")
            json_end = (response.content or "").rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                return json.loads(response.content[json_start:json_end])
        except Exception:
            pass

        return {"assessment": "needs_more_work", "issues": [], "next_action": None}

    async def _recover_from_error(
        self, tc: ToolCall, result: ToolResult, context: list[str]
    ) -> dict[str, Any] | None:
        """Try to recover from a failed tool call."""
        prompt = RECOVERY_PROMPT.format(
            tool_name=tc.name,
            arguments=tc.arguments,
            error=result.error or "Unknown error",
            context="\n".join(context[-3:]) or "No prior context",
        )

        try:
            response = await self.llm.chat(
                messages=[
                    Message(role=Role.SYSTEM, content="You are an error recovery module. Respond with JSON only."),
                    Message(role=Role.USER, content=prompt),
                ],
                temperature=0.3,
                max_tokens=500,
            )

            json_start = (response.content or "").find("{")
            json_end = (response.content or "").rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                recovery = json.loads(response.content[json_start:json_end])

                # Try alternative tool if suggested
                alt_tool = recovery.get("alternative_tool")
                alt_args = recovery.get("alternative_args")
                if alt_tool and alt_args:
                    alt_result = await self.tools.execute(alt_tool, alt_args)
                    if alt_result.is_success:
                        return {
                            "name": alt_tool,
                            "success": True,
                            "output": alt_result.to_llm_message(),
                            "status": "recovered",
                        }
        except Exception:
            pass

        return None

    async def _get_final_answer(self, messages: list[Message]) -> str:
        """Force the agent to give a final answer."""
        messages = messages + [
            Message(
                role=Role.USER,
                content="Please provide your final, complete answer now. Summarize everything.",
            )
        ]

        response = await self.llm.chat(
            messages=messages,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        return response.content or "I was unable to generate a complete response."
