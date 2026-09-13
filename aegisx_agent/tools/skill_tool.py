"""Skill library tool — progressive disclosure over the agent's learned skills.

The system prompt only carries skill names and descriptions; the agent calls
this tool to pull the full step-by-step instructions for the skill it needs.
Loading a skill also records the usage so the library can rank by success rate.
"""

from __future__ import annotations

from typing import Any

from aegisx_agent.skills.manager import SkillManager
from aegisx_agent.skills.skill import Skill
from aegisx_agent.tools.base import Tool, ToolResult, ToolStatus


class SkillTool(Tool):
    """List, search, and load reusable skills from the agent's skill library."""

    def __init__(self, skill_manager: SkillManager) -> None:
        super().__init__(
            name="skill",
            description=(
                "Access the agent's reusable skill library (procedural memory). "
                "Actions: 'list' shows every skill with its description, 'search' "
                "finds skills by keyword, and 'load' returns the full step-by-step "
                "instructions for one skill. Load a skill before starting a task "
                "that it covers."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "search", "load"],
                        "description": "Skill library action",
                    },
                    "query": {
                        "type": "string",
                        "description": "Keyword matched against name, description, or tags",
                    },
                    "name": {
                        "type": "string",
                        "description": "Exact or partial skill name (for 'load')",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results to return (default: 20)",
                        "default": 20,
                    },
                },
                "required": ["action"],
            },
        )
        self._skills = skill_manager

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = kwargs.get("action", "list")
        limit = kwargs.get("limit", 20)

        match action:
            case "list":
                return self._list_skills(limit)
            case "search":
                return self._search_skills(kwargs.get("query", ""), limit)
            case "load":
                return self._load_skill(kwargs.get("name", ""))
            case _:
                return ToolResult(
                    status=ToolStatus.ERROR,
                    output="",
                    error=f"Unknown action: {action}. Expected one of: list, search, load",
                )

    def _list_skills(self, limit: int) -> ToolResult:
        summaries = self._skills.list_summaries()
        if not summaries:
            return ToolResult(
                status=ToolStatus.SUCCESS,
                output=(
                    "No skills in the library yet. "
                    "Skills are captured automatically after complex tasks."
                ),
            )

        lines = [f"{len(summaries)} skill(s) in the library:", ""]
        for summary in summaries[:limit]:
            lines.append(f"- {summary['name']} [{summary['category']}]: {summary['description']}")
        if len(summaries) > limit:
            lines.append(f"... and {len(summaries) - limit} more")
        return ToolResult(status=ToolStatus.SUCCESS, output="\n".join(lines))

    def _search_skills(self, query: str, limit: int) -> ToolResult:
        if not query.strip():
            return ToolResult(
                status=ToolStatus.ERROR,
                output="",
                error="'query' is required for the search action",
            )

        results = self._skills.search(query)[:limit]
        if not results:
            return ToolResult(
                status=ToolStatus.SUCCESS, output=f"No skills match '{query}'."
            )

        lines = [f"{len(results)} match(es) for '{query}':", ""]
        for skill in results:
            lines.append(f"- {skill.name}: {skill.description}")
        return ToolResult(status=ToolStatus.SUCCESS, output="\n".join(lines))

    def _load_skill(self, name: str) -> ToolResult:
        if not name.strip():
            return ToolResult(
                status=ToolStatus.ERROR, output="", error="'name' is required for the load action"
            )

        skill: Skill | None = self._skills.get(name) or self._match_one(name)
        if skill is None:
            return ToolResult(
                status=ToolStatus.ERROR,
                output="",
                error=(
                    f"Skill not found: {name}. "
                    "Use action 'list' or 'search' to find the right name."
                ),
            )

        self._skills.record_use(skill.name, success=True)
        return ToolResult(status=ToolStatus.SUCCESS, output=skill.to_prompt())

    def _match_one(self, name: str) -> Skill | None:
        """Fall back to the best keyword match for a partial skill name."""
        matches = self._skills.search(name)
        return matches[0] if matches else None
