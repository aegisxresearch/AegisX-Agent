"""Reliability fixes: streaming retry on 5xx and tool-output truncation."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from aegisx_agent.llm.base import LLMResponse, Message, Role
from aegisx_agent.llm.openai_provider import OpenAIProvider
from aegisx_agent.tools.base import ToolResult, ToolStatus

# === tool output truncation ===


def test_small_output_passes_through() -> None:
    result = ToolResult(status=ToolStatus.SUCCESS, output="short answer")
    assert result.to_llm_message() == "short answer"


def test_huge_output_is_truncated_with_tail_note() -> None:
    result = ToolResult(status=ToolStatus.SUCCESS, output="x" * 50_000)
    text = result.to_llm_message()

    assert len(text) < 17_500
    assert text.startswith("x" * 100)
    assert "output truncated" in text
    assert "more characters" in text
    assert str(50_000 - result.MAX_LLM_OUTPUT_CHARS) in text or "more characters" in text


def test_error_results_are_truncated_too() -> None:
    result = ToolResult(
        status=ToolStatus.ERROR, output="e" * 40_000, error="boom"
    )
    text = result.to_llm_message()

    assert text.startswith("Tool Error (error): boom")
    assert "output truncated" in text


# === streaming retry on transient 5xx ===


class _FakeStreamResponse:
    def __init__(self, status: int, events: list[str]) -> None:
        self.status_code = status
        self._events = events
        self.is_error = status >= 400
        self.is_success = status < 400
        self._content = b""
        self.request = httpx.Request("POST", "https://x/v1/chat/completions")

    async def aread(self) -> bytes:
        return b""

    def raise_for_status(self) -> None:
        if self.is_error:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=self.request, response=self  # type: ignore[arg-type]
            )

    async def aiter_lines(self):
        for event in self._events:
            yield event

    async def __aenter__(self) -> _FakeStreamResponse:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class _FakeStreamClient:
    """Returns a 520 first, then a working stream."""

    def __init__(self, calls: list[int]) -> None:
        self._calls = calls

    async def __aenter__(self) -> _FakeStreamClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    def stream(self, *args: Any, **kwargs: Any) -> _FakeStreamResponse:
        self._calls.append(1)
        if len(self._calls) == 1:
            return _FakeStreamResponse(520, [])
        done = json.dumps({
            "choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}],
        })
        return _FakeStreamResponse(200, [f"data: {done}", "data: [DONE]"])


@pytest.mark.asyncio()
async def test_stream_retries_transient_5xx(monkeypatch: Any) -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeStreamClient(calls))

    provider = OpenAIProvider(model="m", api_key="k", base_url="https://x/v1")
    pieces: list[Any] = []
    async for item in provider.stream_chat(
        messages=[Message(role=Role.USER, content="hai")]
    ):
        pieces.append(item)

    assert len(calls) == 2  # first 520, second success
    assert sleeps  # backed off between attempts
    final = pieces[-1]
    assert isinstance(final, LLMResponse)
    assert final.content == "ok"


@pytest.mark.asyncio()
async def test_stream_does_not_retry_4xx(monkeypatch: Any) -> None:
    calls: list[int] = []

    class _Client400:
        async def __aenter__(self) -> _Client400:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        def stream(self, *args: Any, **kwargs: Any) -> _FakeStreamResponse:
            calls.append(1)
            return _FakeStreamResponse(400, [])

    # Simpler: build a response whose aread surfaces the JSON body.
    class _Resp400(_FakeStreamResponse):
        def __init__(self) -> None:
            super().__init__(400, [])
            self._content = json.dumps(
                {"error": {"message": "bad request"}}
            ).encode()

        @property
        def text(self) -> str:
            return self._content.decode()

        @property
        def reason_phrase(self) -> str:
            return "Bad Request"

    class _Client(_Client400):
        def stream(self, *args: Any, **kwargs: Any) -> _FakeStreamResponse:
            calls.append(1)
            return _Resp400()

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Client())

    provider = OpenAIProvider(model="m", api_key="k", base_url="https://x/v1")
    with pytest.raises(httpx.HTTPStatusError):
        async for _ in provider.stream_chat(
            messages=[Message(role=Role.USER, content="hai")]
        ):
            pass

    assert len(calls) == 1  # no retry for client errors
