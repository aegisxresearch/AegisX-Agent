"""Subagent delegation — a nested agent run inside one tool call.

``spawn_subagent`` decomposes a task: the parent hands a self-contained job to
a child agent that gets its own message history, its own iteration loop, and
— critically — a **restricted tool set**. Everything about the child is a
subset of the parent:

- **Tools**: only a whitelist (safe defaults, or an explicit list the model
  requested). Unknown names are filtered out; nothing else is invented.
- **Permissions**: the child registry shares the parent's ``PermissionGate``,
  so every child call is re-evaluated by the real gate against the real tool.
  The gate stays authoritative; delegation is not a permission bypass.
- **Budget**: the child loop's ``max_iterations`` *is* the step budget, so
  the cap is structural, not prompt-hopeful.
- **Depth**: a child below ``max_depth`` gets its own ``spawn_subagent`` for
  further decomposition; the deepest generation does not. ``max_depth=1``
  disables nesting entirely.
- **Cost**: the child loops over the parent's (usage-tracked) LLM via a
  factory resolved at call time, so child tokens land in the same usage
  log as the parent's, under the same run id.

The spawn itself is ``SAFE``: it grants no capability the caller did not
already have, because each actual side effect is gated on its own tool.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from aegisx_agent.core.loop import AgenticLoop
from aegisx_agent.llm.base import LLMProvider, Message, Role
from aegisx_agent.security.permissions import PermissionGate
from aegisx_agent.tools.base import Tool, ToolResult, ToolRisk, ToolStatus
from aegisx_agent.tools.registry import ToolRegistry

#: Tools a child gets when the parent does not restrict them: the read-only
#: builtins. Delegation is for research and computation by default; anything
#: with side effects must be requested explicitly (and still passes the gate).
DEFAULT_SUBAGENT_TOOLS: tuple[str, ...] = ("calculator", "datetime")

SUBAGENT_SYSTEM_PROMPT = """You are a focused sub-agent completing one task \
inside a larger job.

You are a working agent with tools and a hard step budget of {max_steps}. \
Work autonomously: nobody will answer follow-up questions mid-run.

Available tools: {tool_names}

Rules:
1. Use tools when they help; skip them when the answer is already known.
2. Stay strictly inside the task you were given. Do not broaden it.
3. Your final response is the deliverable the parent agent will read. Make it \
complete and self-contained: state the result, then the key facts behind it.
4. If the task cannot be completed with the available tools, say exactly what \
is missing instead of guessing.
"""


@dataclass(frozen=True)
class SubagentResult:
    """Structured outcome of one child run (kept out of the LLM-facing text)."""

    answer: str
    steps: int
    tool_calls: int
    total_tokens: int
    duration_seconds: float
    tools_given: list[str]
    budget_exhausted: bool


class SubagentTool(Tool):
    """Run a nested agent on a task and return its final answer.

    The LLM is taken from ``llm_factory`` at call time rather than captured at
    construction, so children always use the agent's *current* provider —
    including the usage-tracking wrapper and mid-session provider switches.
    """

    def __init__(
        self,
        *,
        llm_factory: Callable[[], LLMProvider],
        parent_registry: ToolRegistry | None = None,
        gate: PermissionGate | None = None,
        max_steps: int = 8,
        max_depth: int = 2,
        timeout: float = 120.0,
        depth: int = 1,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(
            name="spawn_subagent",
            description=(
                "Delegate a self-contained subtask to a focused sub-agent with its "
                "own tool set and step budget, and get its final report back. Use "
                "for decomposable work: research, multi-step computation, drafting. "
                "Pass 'tools' (comma-separated) only if the defaults are not enough."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Complete, self-contained description of the subtask.",
                    },
                    "tools": {
                        "type": "string",
                        "description": (
                            "Optional comma-separated tool names for the sub-agent. "
                            f"Defaults to: {', '.join(DEFAULT_SUBAGENT_TOOLS)}"
                        ),
                    },
                },
                "required": ["task"],
            },
            risk=ToolRisk.SAFE,
        )
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if max_depth < 1:
            raise ValueError("max_depth must be at least 1")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._llm_factory = llm_factory
        self._parent_registry = parent_registry
        self._gate = gate
        self._on_progress = on_progress
        self.max_steps = max_steps
        self.max_depth = max_depth
        self.timeout = timeout
        self.depth = depth
        self.temperature = temperature
        self.max_tokens = max_tokens

    # === Construction helpers ========================================== #

    def _resolve_tools(self, requested: Any) -> tuple[list[str], str]:
        """Pick the child's tool names from the parent registry.

        Returns the resolved names plus a note when the request needed
        correcting, so the parent model learns what actually happened.
        """
        if isinstance(requested, (list, tuple)):
            requested = ",".join(str(item) for item in requested)
        requested = str(requested) if requested else ""
        parent = self._parent_registry
        known = {tool.name for tool in parent.list_tools()} if parent else set()

        if requested.strip():
            wanted = [name.strip() for name in requested.split(",") if name.strip()]
            available = [name for name in wanted if name in known]
            if available:
                note = ""
                missing = [name for name in wanted if name not in known]
                if missing:
                    note = f"requested tools not found and skipped: {', '.join(missing)}"
                return available, note

        defaults = [name for name in DEFAULT_SUBAGENT_TOOLS if not known or name in known]
        note = ""
        if requested.strip():
            note = "no requested tools exist; fell back to the default set"
        return defaults, note

    def _child_registry(self, tool_names: list[str]) -> ToolRegistry:
        """Build the child's registry: whitelisted tools + the same gate.

        Tools are shared instances, not copies: they are the same process-local
        objects the parent uses, so every child call is gated and audited
        exactly like a parent call.
        """
        registry = ToolRegistry(gate=self._gate)
        if self._parent_registry is not None:
            for name in tool_names:
                tool = self._parent_registry.get(name)
                if tool is not None:
                    registry.register(tool)

        # A child under the depth cap can delegate further — its spawner is
        # bound to the *child* registry, so nesting only ever narrows. The
        # progress hook flows down too: nested delegations report depth-tagged
        # events to the same observer.
        if self.depth < self.max_depth:
            registry.register(
                SubagentTool(
                    llm_factory=self._llm_factory,
                    parent_registry=registry,
                    gate=self._gate,
                    max_steps=self.max_steps,
                    max_depth=self.max_depth,
                    timeout=self.timeout,
                    depth=self.depth + 1,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    on_progress=self._on_progress,
                )
            )
        return registry

    # === Execution ====================================================== #

    def _emit(self, event: str) -> None:
        """Forward one progress line to the observer, if there is one."""
        if self._on_progress is not None:
            try:
                self._on_progress(event)
            except Exception:  # noqa: BLE001 - telemetry must never break a run
                pass

    async def execute(self, **kwargs: Any) -> ToolResult:
        task = str(kwargs.get("task") or "").strip()
        if not task:
            return ToolResult(
                status=ToolStatus.ERROR,
                output="",
                error="spawn_subagent requires a non-empty 'task' string.",
            )

        tool_names, note = self._resolve_tools(kwargs.get("tools"))
        registry = self._child_registry(tool_names)
        loop = AgenticLoop(
            llm=self._llm_factory(),
            tools=registry,
            max_iterations=self.max_steps,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            enable_reflection=False,
            enable_recovery=False,
            parallel_tools=False,
        )
        system_prompt = SUBAGENT_SYSTEM_PROMPT.format(
            max_steps=self.max_steps,
            tool_names=", ".join(tool.name for tool in registry.list_tools()) or "(none)",
        )
        messages = [Message(role=Role.USER, content=task)]

        self._emit(
            f"⏵ subagent (depth {self.depth}, budget {self.max_steps}): {task[:80]}"
        )

        def _on_child_tool(name: str, success: bool) -> None:
            self._emit(
                f"  ⏳ subagent step: {name} {'✅' if success else '❌'}"
            )

        started = time.monotonic()
        try:
            answer, trace = await asyncio.wait_for(
                loop.run(
                    messages=messages,
                    system_prompt=system_prompt,
                    on_tool_result=_on_child_tool,
                ),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            self._emit(
                f"  ⏹ subagent (depth {self.depth}) timed out after {self.timeout:g}s"
            )
            return ToolResult(
                status=ToolStatus.TIMEOUT,
                output="",
                error=(
                    f"Subagent timed out after {self.timeout:g}s while working on: "
                    f"{task[:120]}"
                ),
                metadata={
                    "depth": self.depth,
                    "tools_given": tool_names,
                },
            )

        duration = time.monotonic() - started
        budget_exhausted = trace.total_tool_calls >= self.max_steps
        final = answer.strip() or "Subagent produced no answer."

        cost = (
            f"⏵ subagent (depth {self.depth}) done: "
            f"{trace.total_tool_calls} tool calls, "
            f"{trace.total_tokens} tokens, {duration:.1f}s"
        )
        self._emit(cost)

        if note:
            final = f"[subagent note: {note}]\n{final}"
        if budget_exhausted and trace.total_tool_calls == self.max_steps:
            # The child stopped on the cap, not because it finished. Say so.
            final = (
                f"{final}\n"
                f"[subagent stopped at its {self.max_steps}-step budget; "
                "the task may be incomplete]"
            )

        result = SubagentResult(
            answer=final,
            steps=len(trace.steps),
            tool_calls=trace.total_tool_calls,
            total_tokens=trace.total_tokens,
            duration_seconds=duration,
            tools_given=tool_names,
            budget_exhausted=budget_exhausted,
        )
        return ToolResult(
            status=ToolStatus.SUCCESS,
            output=result.answer,
            metadata={
                "depth": self.depth,
                "steps": result.steps,
                "tool_calls": result.tool_calls,
                "total_tokens": result.total_tokens,
                "duration_seconds": round(result.duration_seconds, 3),
                "tools_given": result.tools_given,
                "budget_exhausted": result.budget_exhausted,
            },
        )
