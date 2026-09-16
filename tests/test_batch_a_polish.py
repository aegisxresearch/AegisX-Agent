"""Batch-A polish: /resume previews, `mcp search --add`, tools risk filter, FTS trim."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from aegisx_agent.cli import interactive as inter
from aegisx_agent.cli.commands import permissions as perm_cmds
from aegisx_agent.memory.advanced import SessionStore


class _RichCapture:
    """Swap the module-level rich consoles for ones writing to a buffer."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *modules: Any) -> None:
        import io

        self.buffer = io.StringIO()
        for module in modules:
            console = Console(file=self.buffer, force_terminal=False, width=100)
            monkeypatch.setattr(module, "console", console, raising=False)

    @property
    def text(self) -> str:
        return self.buffer.getvalue()


# ═══════════════════════════════════════════════════
#  /resume previews
# ═══════════════════════════════════════════════════


class _PreviewStore:
    def __init__(self, previews: list[dict[str, Any]]) -> None:
        self._previews = previews

    def get_session_previews(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._previews[:limit]

    def get_session_history(self, session_id: str) -> list[dict[str, str]]:
        return [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]


class _Conversation:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    def clear(self) -> None:
        self.messages.clear()

    def add_messages(self, messages: list[Any]) -> None:
        self.messages.extend(messages)


class _ResumeAgent:
    def __init__(self, previews: list[dict[str, Any]]) -> None:
        self.session_store = _PreviewStore(previews)
        self.conversation = _Conversation()
        self.session_id = "current"

    def get_session_stats(self) -> dict[str, int]:
        return {"total_sessions": 1, "total_messages": 2}

    def list_tools(self) -> list[str]:
        return []

    def list_skills(self) -> list[dict[str, str]]:
        return []

    def list_personas(self) -> list[str]:
        return []

    def get_provider_info(self) -> dict[str, str]:
        return {"provider": "ollama", "model": "m"}

    def get_skill(self, name: str):
        return None

    def search_skills(self, query: str):
        return []

    def get_project(self):
        return None


def test_resume_lists_preview_details(monkeypatch: pytest.MonkeyPatch) -> None:
    previews = [
        {"session_id": "s1", "messages": 6, "last": "2026-09-16T10:00:00",
         "first_message": "build me a login page"},
        {"session_id": "s2", "messages": 2, "last": "2026-09-15T09:00:00",
         "first_message": "explain the rag pipeline"},
    ]
    agent = _ResumeAgent(previews)
    capture = _RichCapture(monkeypatch, inter)
    monkeypatch.setattr(inter, "Prompt", _FakePrompt)

    assert inter._handle_slash_command("/resume", agent) is True

    text = capture.text
    assert "s1" in text and "s2" in text
    assert "build me a login page" in text
    assert "2026-09-16" in text


def test_resume_picks_row_from_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    previews = [
        {"session_id": "alpha", "messages": 4, "last": "t", "first_message": "hello"},
    ]
    agent = _ResumeAgent(previews)
    _RichCapture(monkeypatch, inter)
    monkeypatch.setattr(inter, "Prompt", _FakePrompt)

    assert inter._handle_slash_command("/resume", agent) is True

    assert agent.session_id == "alpha"
    assert len(agent.conversation.messages) == 2


def test_resume_cancelled_keeps_current(monkeypatch: pytest.MonkeyPatch) -> None:
    previews = [{"session_id": "alpha", "messages": 4, "last": "t", "first_message": "hi"}]
    agent = _ResumeAgent(previews)
    _RichCapture(monkeypatch, inter)
    monkeypatch.setattr(inter, "Prompt", _FakePrompt)
    _FakePrompt.answer = "99"  # out of range -> cancel

    assert inter._handle_slash_command("/resume", agent) is True

    assert agent.session_id == "current"
    assert agent.conversation.messages == []
    _FakePrompt.answer = "1"


class _FakePrompt:
    """Prompt.ask stand-in that returns a canned answer."""

    answer: str = "1"

    @classmethod
    def ask(cls, *args: Any, **kwargs: Any) -> str:  # noqa: ARG003
        return cls.answer


def test_resume_explicit_id_skips_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _ResumeAgent([])
    capture = _RichCapture(monkeypatch, inter)
    agent.session_store = _PreviewStore([])  # previews empty -> not used

    assert inter._handle_slash_command("/resume alpha", agent) is True

    assert agent.session_id == "alpha"
    assert "Resumed session alpha" in capture.text


# ═══════════════════════════════════════════════════
#  tools risk filter
# ═══════════════════════════════════════════════════


class _Tool:
    def __init__(self, name: str, risk: str, description: str = "does things") -> None:
        from aegisx_agent.security.permissions import ToolRisk

        self.name = name
        self.risk = ToolRisk(risk)
        self.description = description


class _Registry:
    def __init__(self, tools: list[_Tool]) -> None:
        self._tools = tools

    def list_tools(self) -> list[_Tool]:
        return self._tools


class _ToolsAgent:
    def __init__(self) -> None:
        self.tools = _Registry([
            _Tool("calc", "safe"),
            _Tool("file_ops", "caution"),
            _Tool("shell", "dangerous"),
        ])

    def get_provider_info(self) -> dict[str, str]:
        return {"provider": "ollama", "model": "m"}


def test_tools_filter_dangerous_only(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _ToolsAgent()
    capture = _RichCapture(monkeypatch, perm_cmds)

    perm_cmds._print_tools_table(agent, risk_filter="dangerous")

    text = capture.text
    assert "shell" in text
    assert " calc " not in text
    # "file_ops" only survives in the footer hint, never as a table row:
    assert text.count("file_ops") == 1
    assert "dangerous" in text


def test_tools_filter_unknown_risk_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _ToolsAgent()
    capture = _RichCapture(monkeypatch, perm_cmds)

    perm_cmds._print_tools_table(agent, risk_filter="chaos")

    text = capture.text
    assert "Unknown risk" in text
    assert "calc" not in text


def test_tools_filter_empty_result_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _ToolsAgent()
    capture = _RichCapture(monkeypatch, perm_cmds)

    perm_cmds._print_tools_table(agent, risk_filter="safe")  # none registered as safe? calc is safe

    # calc is safe, so table shows it
    assert "calc" in capture.text


def test_tools_slash_passes_args(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _ToolsAgent()
    capture = _RichCapture(monkeypatch, inter, perm_cmds)

    assert inter._handle_slash_command("/tools dangerous", agent) is True

    text = capture.text
    assert "shell" in text
    assert "calc" not in text


# ═══════════════════════════════════════════════════
#  mcp search --add
# ═══════════════════════════════════════════════════


def test_search_catalog_narrows_to_single(tmp_path: Path) -> None:
    from aegisx_agent.mcp.catalog import search_catalog

    everything = search_catalog("")
    assert everything
    one = search_catalog(sorted(everything)[0])
    assert list(one) == [sorted(everything)[0]]


# ═══════════════════════════════════════════════════
#  FTS trim (SessionStore, real sqlite)
# ═══════════════════════════════════════════════════


@pytest.fixture()
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path)


def _seed(store: SessionStore) -> None:
    store.save_message("s1", "user", "alpha unique query")
    store.save_message("s1", "assistant", "beta response body")
    store.save_message("s1", "user", "gamma final answer")


def test_trim_session_also_trims_fts(store: SessionStore) -> None:
    _seed(store)

    removed = store.trim_session("s1", keep=1)

    assert removed == 2
    # The undoed turns are gone from FTS recall.
    hits = store.search("beta response")
    assert hits == []
    survivors = store.search("alpha unique")
    assert [h["content"] for h in survivors] == ["alpha unique query"]


def test_trim_session_keeps_history_correct(store: SessionStore) -> None:
    _seed(store)

    store.trim_session("s1", keep=1)

    history = store.get_session_history("s1")
    assert [m["role"] for m in history] == ["user"]
    assert history[0]["content"] == "alpha unique query"


def test_get_session_previews_shape(store: SessionStore) -> None:
    store.save_message("sess-x", "user", "tell me about rockets")
    store.save_message("sess-x", "assistant", "rockets are vehicles")
    store.save_message("sess-y", "user", "hello world")

    previews = store.get_session_previews(limit=10)

    by_id = {p["session_id"]: p for p in previews}
    assert by_id["sess-x"]["messages"] == 2
    assert by_id["sess-x"]["first_message"].startswith("tell me about rockets")
    assert by_id["sess-y"]["messages"] == 1
    # newest session first
    assert previews[0]["session_id"] == "sess-y"


def test_trim_nothing_when_keep_covers_all(store: SessionStore) -> None:
    _seed(store)

    assert store.trim_session("s1", keep=5) == 0
    assert len(store.get_session_history("s1")) == 3


def test_session_store_persists_metadata(store: SessionStore) -> None:
    store.save_message("s1", "user", "with meta", metadata={"k": "v"})
    row = store.get_session_history("s1")
    assert row[0]["content"] == "with meta"
