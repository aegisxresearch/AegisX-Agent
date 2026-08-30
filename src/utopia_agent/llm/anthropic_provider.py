"""Anthropic Claude LLM provider."""

from __future__ import annotations

from typing import Any

import httpx

from utopia_agent.llm.base import LLMProvider, LLMResponse, Message, ToolCall


class AnthropicProvider(LLMProvider):
    """Anthropic Claude API provider."""

    def __init__(self, model: str = "claude-sonnet-4-20250514", api_key: str = "", **kwargs: Any) -> None:
        super().__init__(model, **kwargs)
        self.api_key = api_key
        self.base_url = kwargs.get("base_url", "https://api.anthropic.com/v1")

    def _convert_messages(
        self, messages: list[Message]
    ) -> tuple[str, list[dict[str, Any]]]:
        """Convert messages to Anthropic format. Extracts system prompt."""
        system_prompt = ""
        converted = []
        for m in messages:
            if m.role.value == "system":
                system_prompt = m.content
            elif m.role.value == "tool":
                converted.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m.tool_call_id or "",
                                "content": m.content,
                            }
                        ],
                    }
                )
            else:
                converted.append({"role": m.role.value, "content": m.content})
        return system_prompt, converted

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
                "  export UTOPIA_ANTHROPIC_API_KEY=sk-ant-...\n\n"
                "Run 'utopia config-info' to see current settings."
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
                tool_calls.append(
                    ToolCall(
                        id=part["id"],
                        name=part["name"],
                        arguments=str(part.get("input", {})),
                    )
                )

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
    ):
        """Stream Anthropic response."""
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
                import json

                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        chunk = json.loads(line[6:])
                        if chunk.get("type") == "content_block_delta":
                            delta = chunk.get("delta", {})
                            if delta.get("type") == "text_delta":
                                yield delta.get("text", "")
