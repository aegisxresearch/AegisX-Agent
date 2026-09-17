"""Forcing tools on real work, and surviving routers that answer 200 with junk."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from support import EchoTool, ScriptedLLM, run

from aegisx_agent.core.loop import AgenticLoop
from aegisx_agent.llm.base import LLMProviderError, LLMResponse, Message, Role, ToolCall
from aegisx_agent.llm.openai_provider import OpenAIProvider
from aegisx_agent.tools.registry import ToolRegistry


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(EchoTool())
    return registry


def _loop(llm: ScriptedLLM, registry: ToolRegistry, **kwargs: Any) -> AgenticLoop:
    return AgenticLoop(
        llm=llm,
        tools=registry,
        max_iterations=4,
        enable_reflection=False,
        enable_recovery=False,
        **kwargs,
    )


def _text(text: str) -> LLMResponse:
    return LLMResponse(content=text, usage={"total_tokens": 3})


def _echo() -> LLMResponse:
    return LLMResponse(
        content="calling echo",
        tool_calls=[ToolCall(id="call_1", name="echo", arguments='{"text": "hi"}')],
        usage={"total_tokens": 5},
    )


# === forcing the first iteration ===


def test_task_shaped_request_requires_a_tool_call() -> None:
    registry = _registry()
    llm = ScriptedLLM([_echo(), _text("done")])

    run(
        _loop(llm, registry).run(
            messages=[Message(role=Role.USER, content="pelajari source code folder ini")],
            system_prompt="system",
            tool_schemas=registry.list_schemas(),
        )
    )

    assert llm.seen_tool_choices[0] == "required"
    # The follow-up iteration must stay free to answer with plain text.
    assert llm.seen_tool_choices[1] is None


def test_small_talk_is_not_forced_into_tools() -> None:
    registry = _registry()
    llm = ScriptedLLM([_text("Hai juga!")])

    answer, _trace = run(
        _loop(llm, registry).run(
            messages=[Message(role=Role.USER, content="hai")],
            system_prompt="system",
            tool_schemas=registry.list_schemas(),
        )
    )

    assert answer == "Hai juga!"
    assert llm.seen_tool_choices == [None]


def test_force_tools_never_disables_forcing() -> None:
    registry = _registry()
    llm = ScriptedLLM([_text("hello")])

    run(
        _loop(llm, registry, force_tools="never").run(
            messages=[Message(role=Role.USER, content="baca file config")],
            system_prompt="system",
            tool_schemas=registry.list_schemas(),
        )
    )

    assert llm.seen_tool_choices == [None]


def test_force_tools_always_forces_even_for_chit_chat() -> None:
    registry = _registry()
    llm = ScriptedLLM([_echo(), _text("done")])

    run(
        _loop(llm, registry, force_tools="always").run(
            messages=[Message(role=Role.USER, content="hai")],
            system_prompt="system",
            tool_schemas=registry.list_schemas(),
        )
    )

    assert llm.seen_tool_choices[0] == "required"


def test_punting_first_answer_is_retried_with_a_forced_tool() -> None:
    """A text-only \"paste your code\" answer is not an answer."""
    registry = _registry()
    llm = ScriptedLLM(
        [
            _text("Silakan bagikan dulu source code yang ingin dipelajari."),
            _echo(),
            _text("selesai"),
        ]
    )

    answer, trace = run(
        _loop(llm, registry).run(
            # Deliberately not task-shaped for the hint regex path: the punt
            # retry must fire on the model's answer alone.
            messages=[Message(role=Role.USER, content="halo")],
            system_prompt="system",
            tool_schemas=registry.list_schemas(),
        )
    )

    assert answer == "selesai"
    assert llm.seen_tool_choices == [None, "required", None]
    assert trace.total_tool_calls == 1
    # Both attempts are billed.
    assert trace.total_tokens == 11


# === provider: required -> auto fallback ===


class _Resp:
    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self._payload = payload
        self.status_code = status
        self.request = httpx.Request("POST", "https://x/v1/chat/completions")
        self._content = json.dumps(payload).encode()

    @property
    def is_success(self) -> bool:
        return self.status_code < 400

    @property
    def is_error(self) -> bool:
        return self.status_code >= 400

    @property
    def text(self) -> str:
        return self._content.decode()

    @property
    def reason_phrase(self) -> str:
        return "Bad Request" if self.status_code == 400 else "OK"

    @property
    def headers(self) -> dict[str, str]:
        return {}

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.is_error:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=self.request, response=self  # type: ignore[arg-type]
            )


class _Client:
    """Records every payload and replays a canned response per call."""

    def __init__(self, responses: list[_Resp], seen: list[dict[str, Any]]) -> None:
        self._responses = responses
        self._seen = seen

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def post(self, *args: Any, **kwargs: Any) -> _Resp:
        # Copy: the provider downgrades ``tool_choice`` in place on rejection.
        self._seen.append(dict(kwargs["json"]))
        return self._responses.pop(0)


def _provider(monkeypatch: Any, responses: list[_Resp]) -> tuple[OpenAIProvider, list[dict]]:
    seen: list[dict[str, Any]] = []

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Client(responses, seen))
    provider = OpenAIProvider(model="m", api_key="k", base_url="https://x/v1")
    return provider, seen


_OK = {
    "choices": [
        {
            "message": {"content": "ok", "tool_calls": None},
            "finish_reason": "stop",
        }
    ],
    "usage": {"total_tokens": 7},
}


@pytest.mark.asyncio()
async def test_chat_downgrades_required_when_server_rejects_it(monkeypatch: Any) -> None:
    responses = [
        _Resp({"error": {"message": "unknown field tool_choice"}}, status=400),
        _Resp(_OK),
    ]
    provider, seen = _provider(monkeypatch, responses)

    response = await provider.chat(
        messages=[Message(role=Role.USER, content="baca file")],
        tools=[{"type": "function", "function": {"name": "echo"}}],
        tool_choice="required",
    )

    assert response.content == "ok"
    assert seen[0]["tool_choice"] == "required"
    assert seen[1]["tool_choice"] == "auto"
    assert provider._rejects_required_tool_choice is True

    # The refusal is remembered: the next turn skips the doomed attempt.
    responses2 = [_Resp(_OK)]
    provider2, seen2 = _provider(monkeypatch, responses2)
    provider2._rejects_required_tool_choice = True
    await provider2.chat(
        messages=[Message(role=Role.USER, content="baca file")],
        tools=[{"type": "function", "function": {"name": "echo"}}],
        tool_choice="required",
    )
    assert seen2[0]["tool_choice"] == "auto"


@pytest.mark.asyncio()
async def test_chat_retries_a_choices_less_200_body(monkeypatch: Any) -> None:
    responses = [
        _Resp({"detail": "upstream unavailable"}),
        _Resp(_OK),
    ]
    provider, seen = _provider(monkeypatch, responses)

    response = await provider.chat(messages=[Message(role=Role.USER, content="hai")])

    assert response.content == "ok"
    assert len(seen) == 2


@pytest.mark.asyncio()
async def test_chat_names_the_body_instead_of_leaking_keyerror(monkeypatch: Any) -> None:
    responses = [_Resp({"detail": "upstream unavailable"}) for _ in range(5)]
    provider, _seen = _provider(monkeypatch, responses)

    with pytest.raises(LLMProviderError) as excinfo:
        await provider.chat(messages=[Message(role=Role.USER, content="hai")])

    assert "upstream unavailable" in str(excinfo.value)
    assert "without a completion payload" in str(excinfo.value)


@pytest.mark.asyncio()
async def test_chat_sends_auto_by_default(monkeypatch: Any) -> None:
    provider, seen = _provider(monkeypatch, [_Resp(_OK)])

    await provider.chat(
        messages=[Message(role=Role.USER, content="hai")],
        tools=[{"type": "function", "function": {"name": "echo"}}],
    )

    assert seen[0]["tool_choice"] == "auto"


# === provider: streaming ===


class _StreamResponse(_Resp):
    def __init__(self, events: list[str], status: int = 200, payload: Any = None) -> None:
        super().__init__(payload or {}, status=status)
        self._events = events

    async def aread(self) -> bytes:
        return self._content

    async def aiter_lines(self):
        for event in self._events:
            yield event

    async def __aenter__(self) -> _StreamResponse:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class _StreamClient:
    def __init__(self, responses: list[_StreamResponse], seen: list[dict[str, Any]]) -> None:
        self._responses = responses
        self._seen = seen

    async def __aenter__(self) -> _StreamClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    def stream(self, *args: Any, **kwargs: Any) -> _StreamResponse:
        # Copy: the provider downgrades ``tool_choice`` in place on rejection.
        self._seen.append(dict(kwargs["json"]))
        return self._responses.pop(0)


def _stream_provider(
    monkeypatch: Any, responses: list[_StreamResponse]
) -> tuple[OpenAIProvider, list[dict]]:
    seen: list[dict[str, Any]] = []

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _StreamClient(responses, seen))
    provider = OpenAIProvider(model="m", api_key="k", base_url="https://x/v1")
    return provider, seen


_DONE = "data: " + json.dumps(
    {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}
)


@pytest.mark.asyncio()
async def test_stream_downgrades_required_when_server_rejects_it(monkeypatch: Any) -> None:
    responses = [
        _StreamResponse([], status=400, payload={"error": {"message": "unknown tool_choice"}}),
        _StreamResponse([_DONE, "data: [DONE]"]),
    ]
    provider, seen = _stream_provider(monkeypatch, responses)

    pieces: list[Any] = []
    async for item in provider.stream_chat(
        messages=[Message(role=Role.USER, content="baca file")],
        tools=[{"type": "function", "function": {"name": "echo"}}],
        tool_choice="required",
    ):
        pieces.append(item)

    assert seen[0]["tool_choice"] == "required"
    assert seen[1]["tool_choice"] == "auto"
    assert pieces[-1].content == "ok"


@pytest.mark.asyncio()
async def test_stream_retries_an_error_envelope(monkeypatch: Any) -> None:
    bad = "data: " + json.dumps({"error": {"message": "upstream unavailable"}})
    responses = [
        _StreamResponse([bad, "data: [DONE]"]),
        _StreamResponse([_DONE, "data: [DONE]"]),
    ]
    provider, seen = _stream_provider(monkeypatch, responses)

    pieces: list[Any] = []
    async for item in provider.stream_chat(
        messages=[Message(role=Role.USER, content="hai")],
        tools=[{"type": "function", "function": {"name": "echo"}}],
    ):
        pieces.append(item)

    assert len(seen) == 2
    assert pieces[-1].content == "ok"


@pytest.mark.asyncio()
async def test_stream_names_the_error_when_retries_run_out(monkeypatch: Any) -> None:
    bad = "data: " + json.dumps({"error": {"message": "upstream unavailable"}})
    responses = [_StreamResponse([bad, "data: [DONE]"]) for _ in range(3)]
    provider, _seen = _stream_provider(monkeypatch, responses)

    with pytest.raises(LLMProviderError) as excinfo:
        async for _ in provider.stream_chat(messages=[Message(role=Role.USER, content="hai")]):
            pass

    assert "upstream unavailable" in str(excinfo.value)
