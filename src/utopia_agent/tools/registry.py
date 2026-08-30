"""Tool registry for managing and invoking tools."""

from __future__ import annotations

import json
from typing import Any

from utopia_agent.tools.base import Tool, ToolResult, ToolStatus


class ToolRegistry:
    """Central registry for all available tools."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Remove a tool by name."""
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def list_tools(self) -> list[Tool]:
        """List all registered tools."""
        return list(self._tools.values())

    def list_schemas(self) -> list[dict[str, Any]]:
        """Get OpenAI function schemas for all tools."""
        return [t.to_schema() for t in self._tools.values()]

    async def execute(self, name: str, arguments: str | dict[str, Any]) -> ToolResult:
        """Execute a tool by name with given arguments."""
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                status=ToolStatus.ERROR,
                output="",
                error=f"Tool '{name}' not found. Available tools: {', '.join(self._tools.keys())}",
            )

        # Parse arguments if string
        if isinstance(arguments, str):
            try:
                args = json.loads(arguments)
            except json.JSONDecodeError:
                args = {"input": arguments}
        else:
            args = arguments

        return await tool.execute(**args)
