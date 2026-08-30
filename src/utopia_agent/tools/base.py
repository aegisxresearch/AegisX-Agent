"""Base tool abstractions for Utopia Agent."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class ToolStatus(str, Enum):
    """Status of a tool execution."""

    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"


@dataclass
class ToolResult:
    """Result of a tool execution."""

    status: ToolStatus
    output: str
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def is_success(self) -> bool:
        return self.status == ToolStatus.SUCCESS

    def to_llm_message(self) -> str:
        """Format result for LLM consumption."""
        if self.is_success:
            return self.output
        return f"Tool Error ({self.status.value}): {self.error}\nOutput: {self.output}"


class Tool(ABC):
    """Abstract base class for all tools."""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any] | None = None,
    ):
        self.name = name
        self.description = description
        self.parameters = parameters or {}

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult:
        """Execute the tool with given arguments."""
        ...

    def to_schema(self) -> dict[str, Any]:
        """Convert tool to OpenAI function calling schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class FunctionTool(Tool):
    """A tool backed by a plain async function."""

    def __init__(
        self,
        name: str,
        description: str,
        func: Callable[..., Any],
        parameters: dict[str, Any] | None = None,
    ):
        super().__init__(name, description, parameters)
        self.func = func

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            import asyncio

            result = self.func(**kwargs)
            if asyncio.iscoroutine(result):
                result = await result
            return ToolResult(status=ToolStatus.SUCCESS, output=str(result))
        except Exception as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                output="",
                error=f"{type(e).__name__}: {e}",
            )


def tool(
    name: str,
    description: str,
    parameters: dict[str, Any] | None = None,
) -> Callable[..., FunctionTool]:
    """Decorator to create a FunctionTool from an async function."""

    def decorator(func: Callable[..., Any]) -> FunctionTool:
        return FunctionTool(name=name, description=description, func=func, parameters=parameters)

    return decorator
