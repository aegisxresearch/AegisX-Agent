"""Dangling-user cleanup + HTTP-200 error body guard + non-streaming 5xx retry."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from aegisx_agent.llm.base import LLMProviderError, Message, Role
from aegisx_agent.llm.openai_provider import OpenAIProvider
from aegisx_agent.memory.store import ConversationMemory

# === ConversationMemory cleanup ===


def test_rollback_turn_drops_trailing_users() -> None:
    memory = ConversationMemory()  # no persistence
    memory.add_messages([
        Message(role=Role.USER, content="q1"),
        Message(role=Role.ASSISTANT, content="a1"),
        Message(role=Role.USER, content="q2"),
        Message(role=Role.USER, content="q3"),
    ])

    dropped = memory.rollback_turn()

    assert dropped == 2
    assert [m.role for m in memory.messages] == [Role.USER, Role.ASSISTANT]


def test_rollback_turn_keeps_answered_pairs() -> None:
    memory = ConversationMemory()
    memory.add_messages([
        Message(role=Role.USER, content="q1"),
        Message(role=Role.ASSISTANT, content="a1"),
    ])

    assert memory.rollback_turn() == 0


def test_collapse_keeps_last_of_each_user_run() -> None:
    seed = [
        Message(role=Role.USER, content="q1"),
        Message(role=Role.USER, content="q2"),
        Message(role=Role.USER, content="q3"),
        Message(role=Role.ASSISTANT, content="a3"),
        Message(role=Role.USER, content="q4"),
        Message(role=Role.USER, content="q5"),
    ]

    cleaned = ConversationMemory._collapse_dangling_users(seed)

    assert [m.content for m in cleaned] == ["q3", "a3", "q5"]


def test_load_cleans_dangling_users_and_persists(tmp_path: Path) -> None:
    persist = tmp_path / "conversation.json"
    persist.write_text(json.dumps({
        "messages": [
            {"role": "user", "content": "q1"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "q3"},
        ],
        "summary": "",
    }))

    memory = ConversationMemory(persist_path=str(persist))

    # q1 superseded by q2; trailing q3 rolled back; state persisted clean.
    assert [(m.role, m.content) for m in memory.messages] == [
        (Role.USER, "q2"),
        (Role.ASSISTANT, "a2"),
    ]
    on_disk = json.loads(persist.read_text())
    assert len(on_disk["messages"]) == 2


# === provider hardening ===


class _FakeResponse:
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status_code = status
        self._body = body
        self.headers: dict[str, str] = {}
        self.is_success = status < 400
        self.request = httpx.Request("POST", "https://x/v1/chat/completions")

    def json(self) -> dict[str, Any]:
        return self._body

    @property
    def text(self) -> str:
        return json.dumps(self._body)


class _FakePostClient:
    """chat() path: async post() returning scripted responses."""

    def __init__(self, responses: list[_FakeResponse], calls: list[int]) -> None:
        self._responses = responses
        self._calls = calls

    async def __aenter__(self) -> _FakePostClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def post(self, *args: Any, **kwargs: Any) -> _FakeResponse:
        self._calls.append(1)
        return self._responses[min(len(self._calls) - 1, len(self._responses) - 1)]


_OK_BODY = {
    "choices": [{
        "message": {"role": "assistant", "content": "ok"},
        "finish_reason": "stop",
    }],
    "usage": {"total_tokens": 5},
}


@pytest.mark.asyncio()
async def test_chat_retries_5xx(monkeypatch: Any) -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakePostClient(
        [_FakeResponse(503, {"error": {"message": "down"}}), _FakeResponse(200, _OK_BODY)],
        calls,
    ))

    provider = OpenAIProvider(model="m", api_key="k", base_url="https://x/v1")
    response = await provider.chat(messages=[Message(role=Role.USER, content="hai")])

    assert len(calls) == 2
    assert sleeps
    assert response.content == "ok"


@pytest.mark.asyncio()
async def test_chat_raises_provider_error_on_200_without_choices(monkeypatch: Any) -> None:
    calls: list[int] = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakePostClient(
        [_FakeResponse(200, {"error": "upstream exploded"})], calls,
    ))

    provider = OpenAIProvider(model="m", api_key="k", base_url="https://x/v1")

    with pytest.raises(LLMProviderError, match="upstream exploded"):
        await provider.chat(messages=[Message(role=Role.USER, content="hai")])
    assert len(calls) == 1  # 200-with-error-body is not retried
