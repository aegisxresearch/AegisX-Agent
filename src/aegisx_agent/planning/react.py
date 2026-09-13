"""ReAct planning loop — Reason + Act for multi-step problem solving."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PlanStep:
    """A single step in an execution plan."""

    step_number: int
    thought: str
    action: str | None = None
    action_input: dict[str, Any] | None = None
    observation: str | None = None
    status: str = "pending"  # pending, running, completed, failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step_number,
            "thought": self.thought,
            "action": self.action,
            "action_input": self.action_input,
            "observation": self.observation,
            "status": self.status,
        }


@dataclass
class ExecutionPlan:
    """A multi-step execution plan."""

    goal: str
    steps: list[PlanStep] = field(default_factory=list)
    current_step: int = 0
    status: str = "pending"  # pending, running, completed, failed

    def add_step(
        self,
        thought: str,
        action: str | None = None,
        action_input: dict[str, Any] | None = None,
    ) -> PlanStep:
        step = PlanStep(
            step_number=len(self.steps) + 1,
            thought=thought,
            action=action,
            action_input=action_input,
        )
        self.steps.append(step)
        return step

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "status": self.status,
            "current_step": self.current_step,
            "steps": [s.to_dict() for s in self.steps],
        }

    def to_prompt(self) -> str:
        """Format plan as a prompt for the LLM."""
        lines = [f"Plan for: {self.goal}", f"Status: {self.status}", ""]
        for step in self.steps:
            status_icon = {"pending": "⏳", "running": "🔄", "completed": "✅", "failed": "❌"}.get(
                step.status, "❓"
            )
            lines.append(f"Step {step.step_number}: {status_icon}")
            lines.append(f"  Thought: {step.thought}")
            if step.action:
                lines.append(f"  Action: {step.action}({step.action_input})")
            if step.observation:
                lines.append(f"  Result: {step.observation[:200]}")
            lines.append("")
        return "\n".join(lines)


class PlanBuilder:
    """Builds execution plans using LLM reasoning."""

    PLANNING_PROMPT = """You are a planning assistant.

Given a user goal, break it down into concrete steps.

For each step, provide:
1. A clear thought explaining what needs to be done
2. An action (tool name) if a tool should be used, or null for reasoning steps
3. Action input as JSON if using a tool

Respond in this exact JSON format:
{
    "goal": "the original goal",
    "steps": [
        {
            "thought": "what to do",
            "action": "tool_name or null",
            "action_input": {"param": "value"} or null
        }
    ]
}

Available tools: {tools}

Goal: {goal}

Provide the plan as JSON:"""

    def build_planning_prompt(self, goal: str, available_tools: list[str]) -> str:
        """Build a prompt for plan generation."""
        tools_str = ", ".join(available_tools) if available_tools else "none (reasoning only)"
        return self.PLANNING_PROMPT.format(tools=tools_str, goal=goal)

    def parse_plan(self, response: str, goal: str) -> ExecutionPlan:
        """Parse LLM response into an ExecutionPlan."""
        import json

        plan = ExecutionPlan(goal=goal)

        try:
            # Try to extract JSON from response
            json_start = response.find("{")
            json_end = response.rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                data = json.loads(response[json_start:json_end])
            else:
                data = json.loads(response)

            for step_data in data.get("steps", []):
                plan.add_step(
                    thought=step_data.get("thought", ""),
                    action=step_data.get("action"),
                    action_input=step_data.get("action_input"),
                )
        except (json.JSONDecodeError, KeyError):
            # Fallback: treat entire response as a single step
            plan.add_step(thought=response.strip())

        return plan
