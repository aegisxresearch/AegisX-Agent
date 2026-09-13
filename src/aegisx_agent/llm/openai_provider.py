"""OpenAI LLM provider."""

from __future__ import annotations

from typing import Any

import httpx

from aegisx_agent.llm.base import LLMProvider, LLMResponse, Message, ToolCall


class OpenAIProvider(LLMProvider):
    """OpenAI API provider."""

    def __init__(self, model: str = "gpt-4o", api_key: str = "", **kwargs: Any) -> None:
        super().__init__(model, **kwargs)
        self.api_key = api_key
        self.base_url = kwargs.get("base_url", "https://api.openai.com/v1")

    def _validate_config(self) -> None:
        """Validate provider configuration before making requests."""
        if not self.api_key:
            raise ValueError(
                "API key not set! Please set your API key:\n\n"
                "  export AEGISX_OPENAI_API_KEY=sk-...\n"
                "  # or for custom providers:\n"
                "  export AEGISX_CUSTOM_API_KEY=your-key\n\n"
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
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_dict() for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        import asyncio

        max_retries = 5
        data: dict[str, Any] = {}
        for attempt in range(max_retries):
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )

                if resp.status_code == 429:
                    # Parse retry-after or use exponential backoff
                    retry_after = resp.headers.get("retry-after")
                    if retry_after and retry_after.isdigit():
                        wait = int(retry_after)
                    else:
                        wait = min(2 ** attempt * 5, 60)  # 5, 10, 20, 40, 60s
                    if attempt < max_retries - 1:
                        await asyncio.sleep(wait)
                        continue

                resp.raise_for_status()
                data = resp.json()
                break

        choice = data["choices"][0]
        message = choice["message"]

        tool_calls = []
        if message.get("tool_calls"):
            tool_calls = [ToolCall.from_openai(tc) for tc in message["tool_calls"]]

        return LLMResponse(
            content=message.get("content"),
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason"),
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
        """Stream chat response (yields text chunks)."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_dict() for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        import json

                        chunk = json.loads(line[6:])
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        if "content" in delta and delta["content"]:
                            yield delta["content"]
