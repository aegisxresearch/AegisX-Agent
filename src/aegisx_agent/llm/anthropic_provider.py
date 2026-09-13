"""Anthropic Claude LLM provider."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from aegisx_agent.llm.base import LLMProvider, LLMResponse, Message, ToolCall


class AnthropicProvider(LLMProvider):
    """Anthropic Claude API provider."""

    def __init__(
        self,
        model: str = "claude-sonnet-4-20250514",
        api_key: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(model, **kwargs)
        self.api_key = api_key
        self.base_url = kwargs.get("base_url", "https://api.anthropic.com/v1")

    def _convert_messages(
        self, messages: list[Message]
    ) -> tuple[str, list[dict[str, Any]]]:
        """Convert messages to Anthropic format.

        Every system message is joined into the single ``system`` string
        Anthropic expects (the agent injects persona plus memory summaries as
        separate system messages), assistant tool calls become ``tool_use``
        blocks, and consecutive tool results are merged into one user turn.
        """
        system_parts: list[str] = []
        converted: list[dict[str, Any]] = []

        for m in messages:
            if m.role.value == "system":
                if m.content:
                    system_parts.append(m.content)
            elif m.role.value == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": m.tool_call_id or "",
                    "content": m.content,
                }
                last = converted[-1] if converted else None
                if (
                    last is not None
                    and last["role"] == "user"
                    and isinstance(last["content"], list)
                    and last["content"][0].get("type") == "tool_result"
                ):
                    last["content"].append(block)
                else:
                    converted.append({"role": "user", "content": [block]})
            elif m.role.value == "assistant" and m.tool_calls:
                blocks: list[dict[str, Any]] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for call in m.tool_calls:
                    function = call.get("function", {})
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call.get("id", ""),
                            "name": function.get("name", ""),
                            "input": self._parse_tool_input(function.get("arguments")),
                        }
                    )
                converted.append({"role": "assistant", "content": blocks})
            else:
                converted.append({"role": m.role.value, "content": m.content})

        return "\n\n".join(part for part in system_parts if part.strip()), converted

    @staticmethod
    def _parse_tool_input(arguments: Any) -> dict[str, Any]:
        """Decode stored tool arguments into the object Anthropic expects."""
        if isinstance(arguments, dict):
            return arguments
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    def _convert_tools(
        self, tools: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Convert OpenAI-style tool schemas to Anthropic format."""
        converted = []
        for t in tools:
            func = t.get("function", {})
            converted.append(
                {
                    "name": func.get("name", ""),
                    "description": func.get("description", ""),
                    "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
                }
            )
        return converted

    def _validate_config(self) -> None:
        if not self.api_key:
            raise ValueError(
                "Anthropic API key not set!\n\n"
                "  export AEGISX_ANTHROPIC_API_KEY=sk-ant-...\n\n"
                "Run 'aegisx config-info' to see current settings."
            )

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        self._validate_config()
        system_prompt, converted_messages = self._convert_messages(messages)

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": converted_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if system_prompt:
            payload["system"] = system_prompt
        if tools:
            payload["tools"] = self._convert_tools(tools)

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self.base_url}/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        content_parts = data.get("content", [])
        text_content = ""
        tool_calls = []

        for part in content_parts:
            if part["type"] == "text":
                text_content += part["text"]
            elif part["type"] == "tool_use":
                tool_calls.append(ToolCall.from_anthropic(part))

        return LLMResponse(
            content=text_content or None,
            tool_calls=tool_calls,
            finish_reason=data.get("stop_reason"),
            usage=data.get("usage", {}),
            raw=data,
        )

    async def stream_chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> AsyncIterator[str | LLMResponse]:
        """Stream Anthropic response (text chunks, then a final response).

        Mirrors ``OpenAIProvider.stream_chat``: text deltas are yielded as
        they arrive, and the last yielded item is an ``LLMResponse`` holding
        the full text, any ``tool_use`` blocks, the stop reason, and usage.
        """
        self._validate_config()
        system_prompt, converted_messages = self._convert_messages(messages)

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": converted_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if system_prompt:
            payload["system"] = system_prompt
        if tools:
            payload["tools"] = self._convert_tools(tools)

        content_parts: list[str] = []
        tool_calls_by_index: dict[int, dict[str, Any]] = {}
        stop_reason: str | None = None
        usage: dict[str, Any] = {}

        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json=payload,
            ) as resp:
                resp.raise_for_status()

                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload_text = line[6:].strip()
                    if not payload_text or payload_text == "[DONE]":
                        # [DONE] is OpenAI's terminator, not Anthropic's — but
                        # some proxies append it to every SSE stream anyway.
                        continue
                    try:
                        chunk = json.loads(payload_text)
                    except json.JSONDecodeError:
                        # Skip keep-alive noise from intermediaries.
                        continue
                    event_type = chunk.get("type", "")

                    if event_type == "message_start":
                        usage.update(chunk.get("message", {}).get("usage", {}))
                    elif event_type == "content_block_start":
                        block = chunk.get("content_block", {})
                        if block.get("type") == "tool_use":
                            tool_calls_by_index[chunk.get("index", 0)] = {
                                "id": block.get("id", ""),
                                "name": block.get("name", ""),
                                "arguments": "",
                            }
                    elif event_type == "content_block_delta":
                        delta = chunk.get("delta", {})
                        if delta.get("type") == "text_delta":
                            piece = delta.get("text", "")
                            if piece:
                                content_parts.append(piece)
                                yield piece
                        elif delta.get("type") == "input_json_delta":
                            index = chunk.get("index", 0)
                            if index in tool_calls_by_index:
                                tool_calls_by_index[index]["arguments"] += delta.get(
                                    "partial_json", ""
                                )
                    elif event_type == "message_delta":
                        stop_reason = chunk.get("delta", {}).get("stop_reason", stop_reason)
                        usage.update(chunk.get("usage", {}))

        tool_calls = [
            ToolCall.from_anthropic(
                {
                    "id": entry["id"],
                    "name": entry["name"],
                    # No partial_json deltas means the tool takes no input.
                    "input": entry["arguments"] or {},
                }
            )
            for _, entry in sorted(tool_calls_by_index.items())
        ]
        full_content = "".join(content_parts) or None

        yield LLMResponse(
            content=full_content,
            tool_calls=tool_calls,
            finish_reason=stop_reason,
            usage=usage,
            raw={},
        )
