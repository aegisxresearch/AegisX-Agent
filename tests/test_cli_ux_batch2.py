"""Batch-2 UX: scoped approvals, /resume, `aegisx init`, and `run --json`."""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from aegisx_agent.cli import interactive as inter
from aegisx_agent.cli import main as cli
from aegisx_agent.core import AegisXAgent
from aegisx_agent.security.permissions import PermissionGate, PermissionMode, PermissionRequest
from aegisx_agent.tools.base import ToolRisk

# === scoped approvals ===


def test_gate_allows_scoped_match_and_denies_outside() -> None:
    gate = PermissionGate(mode=PermissionMode.ASK)
    gate.allow_scoped("editor", "login-register/**")

    inside = PermissionRequest(
        tool="editor", risk=ToolRisk.DANGEROUS,
        summary="write", arguments={"path": "login-register/app.js", "action": "write"},
    )
    outside = PermissionRequest(
        tool="editor", risk=ToolRisk.DANGEROUS,
        summary="write", arguments={"path": "secrets.txt", "action": "write"},
    )
    assert gate.evaluate(inside.tool, inside.risk, inside.arguments).allowed is True
    assert gate.evaluate(outside.tool, outside.risk, outside.arguments).allowed is False


def test_gate_clears_full_allowlist_on_scope() -> None:
    gate = PermissionGate(mode=PermissionMode.ASK)
    gate.allow("editor")
    gate.allow_scoped("editor", "tmp/**")
    request = PermissionRequest(
        tool="editor", risk=ToolRisk.DANGEROUS,
        summary="write", arguments={"path": "other/x.txt", "action": "write"},
    )
    assert gate.evaluate(request.tool, request.risk, request.arguments).allowed is False


def test_default_scope_for_editor_and_shell() -> None:
    gate = PermissionGate(mode=PermissionMode.ASK)
    editor = gate.default_scope_for("editor", {"path": "src/app.py", "action": "write"})
    shell = gate.default_scope_for("shell", {"command": "pytest -q"})
    git_ = gate.default_scope_for("git", {"action": "push"})
    assert editor == "src/**"
    assert shell == "pytest*"
    assert git_ == "push"


# === /resume ===


class _FakeSessionStore:
    """Canned history so /resume can be exercised without a database."""

    def __init__(self, sessions: list[str], history: list[dict[str, str]]) -> None:
        self._sessions = sessions
        self._history = history

    def get_recent_sessions(self, limit: int = 10) -> list[str]:
        return self._sessions[:limit]

    def get_session_history(self, session_id: str) -> list[dict[str, str]]:
        return self._history


class _FakeConversation:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    def clear(self) -> None:
        self.messages.clear()

    def add_messages(self, messages: list[Any]) -> None:
        self.messages.extend(messages)


class _ResumeAgent:
    def __init__(self) -> None:
        self.session_store = _FakeSessionStore(
            ["old1", "old2"],
            [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
        )
        self.conversation = _FakeConversation()
        self.session_id = "current"
        self.calls: list[tuple[str, Any]] = []

    def get_session_stats(self) -> dict[str, int]:
        return {"total_sessions": 2, "total_messages": 4}

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


def test_resume_with_explicit_id_restores_history(capsys) -> None:
    agent = _ResumeAgent()

    assert inter._handle_slash_command("/resume old1", agent) is True

    assert agent.session_id == "old1"
    assert len(agent.conversation.messages) == 2
    text = capsys.readouterr().out
    assert "Resumed session old1" in text


def test_resume_without_sessions_warns(capsys) -> None:
    agent = _ResumeAgent()
    agent.session_store = _FakeSessionStore([], [])

    assert inter._handle_slash_command("/resume", agent) is True

    out = capsys.readouterr().out
    assert "No saved sessions" in out
    assert agent.session_id == "current"


# === aegisx init ===


@pytest.fixture()
def runner(tmp_path, monkeypatch):
    monkeypatch.setenv("AEGISX_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "CONFIG_FILE", tmp_path / "config.json")
    return CliRunner()


def test_init_saves_config_and_writes_agents_md(runner: CliRunner, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    # No local Ollama: the wizard must fall back to its plain default prompt.
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("offline"))
    )

    result = runner.invoke(
        cli.app,
        ["init"],
        input="openai\ngpt-4o-mini\nsk-test-123\n",
    )

    assert result.exit_code == 0, result.output
    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved["provider"] == "openai"
    assert saved["api_key"] == "sk-test-123"
    assert (tmp_path / "AGENTS.md").exists()


def test_init_is_idempotent_about_agents_md(runner: CliRunner, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text("# keep me\n")
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("offline"))
    )

    result = runner.invoke(cli.app, ["init"], input="openai\ngpt-4o-mini\nsk-x\n")

    assert result.exit_code == 0, result.output
    assert (tmp_path / "AGENTS.md").read_text() == "# keep me\n"


# === run --json ===


@pytest.fixture()
def run_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AEGISX_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("AEGISX_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(cli, "_agent", None)
    return tmp_path


def test_run_json_emits_machine_readable_payload(run_env, monkeypatch) -> None:
    from types import SimpleNamespace

    async def fake_chat(self: AegisXAgent, message: str, on_progress: Any = None) -> str:
        return "answer text"

    monkeypatch.setattr(AegisXAgent, "chat", fake_chat)
    monkeypatch.setattr(
        AegisXAgent,
        "last_trace",
        property(lambda self: SimpleNamespace(total_tokens=101, total_tool_calls=3)),
        raising=False,
    )

    runner = CliRunner()
    result = runner.invoke(cli.app, ["run", "task", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["answer"] == "answer text"
    assert payload["tokens"] == 101
    assert payload["tool_calls"] == 3


def test_run_json_error_is_json_not_prose(run_env, monkeypatch) -> None:
    async def boom(self: AegisXAgent, message: str, on_progress: Any = None) -> str:
        raise RuntimeError("provider down")

    monkeypatch.setattr(AegisXAgent, "chat", boom)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["run", "task", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert "provider down" in payload["error"]
