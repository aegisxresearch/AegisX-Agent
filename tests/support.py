"""Shared test doubles for the AegisX Agent suite."""

from __future__ import annotations

import asyncio
from typing import Any

from aegisx_agent.llm.base import LLMProvider, LLMResponse, Message
from aegisx_agent.tools.base import Tool, ToolResult, ToolRisk, ToolStatus


def run(coro: Any) -> Any:
    """Run a coroutine without requiring pytest-asyncio."""
    return asyncio.run(coro)


class EchoTool(Tool):
    """Tool with a declared schema that echoes its ``text`` argument."""

    def __init__(self) -> None:
        super().__init__(
            name="echo",
            description="Echo the given text.",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(status=ToolStatus.SUCCESS, output=f"echo:{kwargs['text']}")


class ExplodingTool(Tool):
    """Tool that always crashes, to prove failures are contained."""

    def __init__(self) -> None:
        super().__init__(
            name="boom",
            description="Always crashes.",
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        raise RuntimeError("kaboom")


class SpyTool(Tool):
    """Tool that records every call, so a test can prove a call never ran.

    It escalates to ``DANGEROUS`` for ``action='delete'``, mirroring how the
    real tools vary their risk per action.
    """

    def __init__(self, name: str = "spy", risk: ToolRisk = ToolRisk.SAFE) -> None:
        super().__init__(
            name=name,
            description="Records the arguments it is called with.",
            parameters={
                "type": "object",
                "properties": {"action": {"type": "string"}},
            },
            risk=risk,
        )
        self.calls: list[dict[str, Any]] = []

    def risk_for(self, arguments: dict[str, Any]) -> ToolRisk:
        if arguments.get("action") == "delete":
            return ToolRisk.DANGEROUS
        return self.risk

    async def execute(self, **kwargs: Any) -> ToolResult:
        self.calls.append(dict(kwargs))
        return ToolResult(status=ToolStatus.SUCCESS, output="ran")


class SchemalessTool(Tool):
    """Tool without a JSON schema — arbitrary kwargs must still pass through."""

    def __init__(self) -> None:
        super().__init__(name="raw", description="No schema declared.", parameters=None)

    async def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(status=ToolStatus.SUCCESS, output=f"raw:{sorted(kwargs)}")


class ScriptedLLM(LLMProvider):
    """LLM stub that replays a fixed list of responses and records the prompts."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__(model="scripted")
        self.responses = list(responses)
        self.seen_messages: list[list[Message]] = []
        self.seen_tool_choices: list[str | None] = []

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tool_choice: str | None = None,
    ) -> LLMResponse:
        self.seen_messages.append(list(messages))
        self.seen_tool_choices.append(tool_choice)
        if not self.responses:
            raise AssertionError("ScriptedLLM ran out of scripted responses")
        return self.responses.pop(0)

    async def stream_chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tool_choice: str | None = None,
    ):
        response = await self.chat(
            messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
        )
        if response.content:
            yield response.content
        yield response

    async def run_streaming(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        on_chunk: Any = None,
        tool_choice: str | None = None,
    ) -> LLMResponse:
        """Replay one scripted response, optionally forwarding its text."""
        response = await self.chat(
            messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
        )
        if on_chunk is not None and response.content:
            # Emit the text in small pieces, like a real stream would.
            for i in range(0, len(response.content), 3):
                on_chunk(response.content[i : i + 3])
        return response
