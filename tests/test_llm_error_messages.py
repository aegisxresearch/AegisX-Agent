"""Provider errors must carry the server's own message, not just a status."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from aegisx_agent.llm.openai_provider import OpenAIProvider, _raise_with_server_message


def _response(
    status: int,
    body: dict[str, Any] | str,
    url: str = "https://api.example.com/v1/chat/completions",
) -> httpx.Response:
    request = httpx.Request("POST", url)
    content = body if isinstance(body, str) else json.dumps(body)
    return httpx.Response(status, content=content.encode(), request=request)


def test_quota_error_surfaces_the_server_message() -> None:
    resp = _response(400, {
        "error": {
            "message": "credit insufficient balance: balance=0 required=58",
            "type": "api_error",
            "code": "insufficient_user_quota",
        }
    })

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        _raise_with_server_message(resp)

    text = str(excinfo.value)
    assert "HTTP 400" in text
    assert "credit insufficient balance: balance=0 required=58" in text


def test_plain_string_error_and_non_json_body() -> None:
    resp = _response(401, {"error": "invalid api key"})
    with pytest.raises(httpx.HTTPStatusError, match="invalid api key"):
        _raise_with_server_message(resp)

    raw = _response(502, "<html>bad gateway</html>")
    with pytest.raises(httpx.HTTPStatusError, match="bad gateway"):
        _raise_with_server_message(raw)


def test_success_is_a_no_op() -> None:
    resp = _response(200, {"choices": []})
    _raise_with_server_message(resp)  # must not raise


@pytest.mark.asyncio()
async def test_chat_error_includes_server_message(monkeypatch: Any) -> None:
    """End-to-end: a 400 from the endpoint propagates its body text."""
    provider = OpenAIProvider(model="m", api_key="k", base_url="https://api.example.com/v1")

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, *args: Any, **kwargs: Any) -> httpx.Response:
            return _response(400, {
                "error": {"message": "insufficient_user_quota: balance=0", "code": "x"}
            })

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    from aegisx_agent.llm.base import Message, Role

    with pytest.raises(httpx.HTTPStatusError, match="balance=0"):
        await provider.chat([Message(role=Role.USER, content="hai")])
