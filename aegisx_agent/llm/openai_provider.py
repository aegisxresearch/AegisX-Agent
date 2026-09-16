"""OpenAI LLM provider."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from aegisx_agent.llm.base import LLMProvider, LLMResponse, Message, ToolCall


def _raise_with_server_message(resp: httpx.Response) -> None:
    """``raise_for_status``, but with the server's own error text included.

    A bare "400 Bad Request" hides the actual problem — quota exhausted,
    unknown model, invalid key — while every OpenAI-compatible server names
    it in the response body. Without this the CLI shows a dead end.
    """
    if resp.is_success:
        return
    detail = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict) and error.get("message"):
                detail = str(error["message"])
            elif isinstance(error, str):
                detail = error
            elif body.get("message"):
                detail = str(body["message"])
            else:
                detail = json.dumps(body)
        else:
            detail = str(body)
    except Exception:  # noqa: BLE001 — body may be empty or non-JSON
        detail = resp.text or ""
    detail = detail.strip()[:300]
    message = f"HTTP {resp.status_code} {resp.reason_phrase} from {resp.request.url}"
    if detail:
        message += f" — {detail}"
    raise httpx.HTTPStatusError(message, request=resp.request, response=resp) from None


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

                _raise_with_server_message(resp)
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
    ) -> AsyncIterator[str | LLMResponse]:
        """Stream a chat completion (yields text chunks, then a final response).

        The last item yielded is always an ``LLMResponse`` carrying the full
        text, any tool calls parsed from ``tool_calls`` deltas, the finish
        reason, and usage. That makes streaming a complete replacement for a
        non-streaming ``chat()`` call — the agent loop can stream every turn
        instead of spending one full request just to probe for tools.
        """
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

        content_parts: list[str] = []
        tool_calls_by_index: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        usage: dict[str, Any] = {}

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
                if resp.is_error:
                    await resp.aread()  # the body names the failure
                    _raise_with_server_message(resp)
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload_text = line[6:]
                    if payload_text.strip() == "[DONE]":
                        break
                    chunk = json.loads(payload_text)

                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]

                    delta = choice.get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        content_parts.append(piece)
                        yield piece

                    for call_delta in delta.get("tool_calls") or []:
                        index = call_delta.get("index", 0)
                        entry = tool_calls_by_index.setdefault(
                            index, {"id": "", "name": "", "arguments": ""}
                        )
                        if call_delta.get("id"):
                            entry["id"] = call_delta["id"]
                        function = call_delta.get("function") or {}
                        if function.get("name"):
                            entry["name"] = (
                                entry["name"] + function["name"]
                                if entry["name"] and not call_delta.get("id")
                                else function["name"]
                            )
                        if function.get("arguments"):
                            entry["arguments"] += function["arguments"]

        tool_calls = [
            ToolCall(
                id=entry["id"] or f"call_{index}",
                name=entry["name"],
                arguments=entry["arguments"],
            )
            for index, entry in sorted(tool_calls_by_index.items())
        ]
        full_content = "".join(content_parts) or None

        yield LLMResponse(
            content=full_content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            raw={},
        )
