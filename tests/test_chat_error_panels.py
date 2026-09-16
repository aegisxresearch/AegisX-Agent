"""Chat errors render the server's message with an actionable hint."""

from __future__ import annotations

from typing import Any

import httpx

from aegisx_agent.cli.interactive import _print_chat_error


def _err(status: int, msg: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://x.example/v1/chat/completions")
    return httpx.HTTPStatusError(
        f"HTTP {status} from https://x.example/v1/chat/completions — {msg}",
        request=request,
        response=httpx.Response(status, request=request),
    )


def _capture(capsys: Any, exc: Exception) -> str:
    _print_chat_error(exc)
    return capsys.readouterr().out


def test_content_blocked_names_the_endpoint_problem(capsys: Any) -> None:
    out = _capture(capsys, _err(400, "content-blocked (request id: X)"))
    assert "Content blocked" in out
    assert "content-blocked" in out
    assert "aegisx init" in out


def test_quota_error_suggests_topup(capsys: Any) -> None:
    out = _capture(capsys, _err(400, "credit insufficient balance: balance=0 required=58"))
    assert "Quota exhausted" in out
    assert "balance=0" in out
    assert "aegisx init" in out


def test_auth_error_shows_key_hint(capsys: Any) -> None:
    out = _capture(capsys, _err(401, "unauthorized client detected"))
    assert "Authentication failed" in out
    assert "config-info" in out


def test_rate_limit_and_connection_hints(capsys: Any) -> None:
    out = _capture(capsys, _err(429, "rate limit exceeded"))
    assert "Rate limited" in out

    out = _capture(capsys, ConnectionError("connection refused"))
    assert "Connection error" in out


def test_generic_error_still_shown(capsys: Any) -> None:
    out = _capture(capsys, RuntimeError("something odd"))
    assert "something odd" in out
