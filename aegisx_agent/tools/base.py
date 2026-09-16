"""Base tool abstractions for AegisX Agent."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ToolStatus(str, Enum):
    """Status of a tool execution."""

    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"


class ToolRisk(str, Enum):
    """How much damage a tool call can do if the model gets it wrong.

    Tools declare this themselves so the permission gate never has to guess
    from a tool's name:

    - ``SAFE`` — read-only, no side effects (search, read, calculate).
    - ``CAUTION`` — writes inside the workspace or makes a network request
      that changes state. Recoverable with git, so it does not need a prompt.
    - ``DANGEROUS`` — arbitrary code or command execution, irreversible
      deletion, or history-rewriting git commands. Requires approval.
    """

    SAFE = "safe"
    CAUTION = "caution"
    DANGEROUS = "dangerous"


#: Total order used to compare risks (e.g. escalate a tool per action).
RISK_ORDER: dict[ToolRisk, int] = {
    ToolRisk.SAFE: 0,
    ToolRisk.CAUTION: 1,
    ToolRisk.DANGEROUS: 2,
}


def highest_risk(*risks: ToolRisk) -> ToolRisk:
    """Return the most severe of ``risks`` (``SAFE`` when called with none)."""
    return max(risks, key=lambda risk: RISK_ORDER[risk], default=ToolRisk.SAFE)


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

    #: Cap on one tool result's size when sent back to the LLM. Full output
    #: stays available to the UI; oversized payloads make some OpenAI-
    #: compatible gateways choke (bad gateway / 520s) and burn tokens.
    MAX_LLM_OUTPUT_CHARS = 16_000

    def to_llm_message(self) -> str:
        """Format result for LLM consumption, truncating oversized output.

        The head carries most signal (file listings, logs); the tail note
        tells the model it may request a narrower view instead of the tool
        being silent about the rest.
        """
        text = self.output if self.is_success else (
            f"Tool Error ({self.status.value}): {self.error}\nOutput: {self.output}"
        )
        limit = self.MAX_LLM_OUTPUT_CHARS
        if len(text) <= limit:
            return text
        head = text[:limit]
        omitted = len(text) - limit
        return (
            f"{head}\n… [output truncated: {omitted} more characters. "
            "Re-run with a narrower query/path/range if you need the rest.]"
        )


class Tool(ABC):
    """Abstract base class for all tools."""

    #: Risk assumed for calls this tool does not explicitly escalate.
    risk: ToolRisk = ToolRisk.SAFE

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any] | None = None,
        risk: ToolRisk = ToolRisk.SAFE,
    ):
        self.name = name
        self.description = description
        self.parameters = parameters or {}
        self.risk = risk

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult:
        """Execute the tool with given arguments."""
        ...

    def risk_for(self, arguments: dict[str, Any]) -> ToolRisk:
        """Risk of this specific call.

        Tools with an ``action`` parameter override this to escalate only the
        destructive actions (for example reading a file is ``SAFE`` while
        deleting it is ``DANGEROUS``). The arguments have already been parsed
        and validated against the tool's schema when this is called.
        """
        return self.risk

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
        risk: ToolRisk = ToolRisk.SAFE,
    ):
        super().__init__(name, description, parameters, risk)
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
    risk: ToolRisk = ToolRisk.SAFE,
) -> Callable[..., FunctionTool]:
    """Decorator to create a FunctionTool from an async function."""

    def decorator(func: Callable[..., Any]) -> FunctionTool:
        return FunctionTool(
            name=name,
            description=description,
            func=func,
            parameters=parameters,
            risk=risk,
        )

    return decorator
