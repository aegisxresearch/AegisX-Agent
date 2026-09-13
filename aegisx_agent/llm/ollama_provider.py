"""Ollama local LLM provider."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from aegisx_agent.llm.base import (
    LLMProvider,
    LLMResponse,
    Message,
    ToolCall,
    normalise_tool_arguments,
)


class OllamaProvider(LLMProvider):
    """Ollama local model provider. Works with any model pulled via ollama pull."""

    def __init__(
        self, model: str = "llama3.1", base_url: str = "http://localhost:11434", **kwargs: Any
    ) -> None:
        super().__init__(model, **kwargs)
        self.base_url = base_url.rstrip("/")

    def _convert_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Convert messages to Ollama format."""
        converted = []
        for m in messages:
            converted.append({"role": m.role.value, "content": m.content})
        return converted

    def _convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert OpenAI-style tool schemas to Ollama format."""
        converted = []
        for t in tools:
            func = t.get("function", {})
            converted.append(
                {
                    "type": "function",
                    "function": {
                        "name": func.get("name", ""),
                        "description": func.get("description", ""),
                        "parameters": func.get("parameters", {"type": "object", "properties": {}}),
                    },
                }
            )
        return converted

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._convert_messages(messages),
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if tools:
            payload["tools"] = self._convert_tools(tools)

        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(f"{self.base_url}/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()

        message = data.get("message", {})
        tool_calls = []

        if message.get("tool_calls"):
            for index, tc in enumerate(message["tool_calls"]):
                func = tc.get("function", {})
                name = func.get("name", "")
                tool_calls.append(
                    ToolCall(
                        id=f"ollama_{index}_{name}",
                        name=name,
                        arguments=normalise_tool_arguments(func.get("arguments", {})),
                    )
                )

        return LLMResponse(
            content=message.get("content"),
            tool_calls=tool_calls,
            finish_reason="stop" if not tool_calls else "tool_calls",
            usage={
                "prompt_tokens": data.get("prompt_eval_count", 0),
                "completion_tokens": data.get("eval_count", 0),
            },
            raw=data,
        )

    async def stream_chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> AsyncIterator[str | LLMResponse]:
        """Stream Ollama response (text chunks, then a final response).

        Ollama streams NDJSON lines rather than SSE frames. Text deltas are
        yielded as they arrive; the last yielded item is an ``LLMResponse``
        with the full text, any tool calls, and token usage from the final
        ``done`` line — so streaming fully replaces a non-streaming call.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._convert_messages(messages),
            "stream": True,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if tools:
            payload["tools"] = self._convert_tools(tools)

        content_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        usage: dict[str, int] = {}

        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream("POST", f"{self.base_url}/api/chat", json=payload) as resp:
                resp.raise_for_status()

                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    message = chunk.get("message") or {}

                    piece = message.get("content", "")
                    if piece:
                        content_parts.append(piece)
                        yield piece

                    for index, call in enumerate(message.get("tool_calls") or []):
                        func = call.get("function", {})
                        tool_calls.append(
                            ToolCall(
                                id=f"ollama_{len(tool_calls)}_{func.get('name', '')}",
                                name=func.get("name", ""),
                                arguments=normalise_tool_arguments(
                                    func.get("arguments", {})
                                ),
                            )
                        )

                    if chunk.get("done"):
                        usage = {
                            "prompt_tokens": chunk.get("prompt_eval_count", 0),
                            "completion_tokens": chunk.get("eval_count", 0),
                        }

        yield LLMResponse(
            content="".join(content_parts) or None,
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else "stop",
            usage=usage,
            raw={},
        )
