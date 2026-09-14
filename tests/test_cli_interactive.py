"""cli/interactive.py: progress animation, the slash dispatch, and the chat loop.

The dispatch is exercised with a stub agent so every branch is reachable
without a provider; ``_run_chat`` runs with monkeypatched input to prove the
loop, the banner, and the error handling behave.
"""

from __future__ import annotations

from typing import Any

import pytest

from aegisx_agent.cli import interactive as inter
from aegisx_agent.cli.commands.code import _handle_code_command  # noqa: F401


class StubAgent:
    """Whatever the interactive layer asks, it answers with canned values."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.config = type("C", (), {"persona": "coder"})()
        self.permission_gate = type("G", (), {"mode": type("M", (), {"value": "ask"})()})()
        self.session_id = "s123"

    def get_provider_info(self) -> dict[str, str]:
        return {"provider": "ollama", "model": "llama3.1"}

    def list_tools(self) -> list[str]:
        return ["a", "b"]

    def list_skills(self) -> list[dict[str, str]]:
        return [{"name": "deploy", "description": "How to deploy"}]

    def list_personas(self) -> list[str]:
        return ["default", "coder"]

    def get_session_stats(self) -> dict[str, int]:
        return {"total_sessions": 3, "total_messages": 40}

    def learn_preference(self, key: str, value: str) -> None:
        self.calls.append(("learn", (key, value)))

    def clear_memory(self) -> None:
        self.calls.append(("clear", ()))

    def set_model(self, model: str) -> None:
        self.calls.append(("set_model", model))

    def set_provider(self, provider: str) -> None:
        self.calls.append(("set_provider", provider))

    def set_persona(self, persona: str) -> None:
        self.calls.append(("set_persona", persona))

    def get_skill(self, name: str):
        return None

    def search_skills(self, query: str):
        return [{"name": "deploy", "description": "How to deploy"}]

    def get_project(self):
        return None


@pytest.fixture()
def agent(capsys: pytest.CaptureFixture[str]) -> StubAgent:
    return StubAgent()


def out(capsys: pytest.CaptureFixture[str]) -> str:
    return capsys.readouterr().out


# === AnimatedProgress ===


def test_animated_progress_frames_and_tool_icons() -> None:
    progress = inter.AnimatedProgress()
    assert progress.get_frame() == ""  # not running yet

    progress.thinking()
    frame = progress.get_frame()
    assert "Thinking" in frame and "⠋" in frame

    progress.tool_call("calculator")
    assert "🔢" in progress.get_frame()
    progress.tool_call("unknown_tool")
    assert "🔧" in progress.get_frame()
    progress.tool_call("shell", "success")
    assert "✅" in progress.get_frame()
    progress.tool_call("shell", "error")
    assert "❌" in progress.get_frame()

    progress.stop()
    assert progress.get_frame() == ""


# === command menu ===


def test_command_menu_lists_and_filters(capsys) -> None:
    inter._show_command_menu()
    full = out(capsys)
    assert "/help" in full and "/quit" in full and "Commands" in full

    inter._show_command_menu("sched")
    assert "/schedule" in out(capsys)
    assert "/quit" not in out(capsys)


# === slash dispatch ===


def test_dispatch_handles_model_provider_persona(agent, capsys) -> None:
    assert inter._handle_slash_command("/model gpt-4o", agent) is True
    assert ("set_model", "gpt-4o") in agent.calls

    inter._handle_slash_command("/model", agent)
    assert "Current model" in out(capsys)

    inter._handle_slash_command("/provider ollama", agent)
    assert ("set_provider", "ollama") in agent.calls

    inter._handle_slash_command("/persona coder", agent)
    assert ("set_persona", "coder") in agent.calls

    inter._handle_slash_command("/persona", agent)
    assert "Available personas" in out(capsys)


def test_dispatch_reports_setter_failures(agent, capsys) -> None:
    def boom(value: str) -> None:
        raise ValueError("nope")

    agent.set_model = boom  # type: ignore[method-assign]
    inter._handle_slash_command("/model bad", agent)
    assert "Could not switch model" in out(capsys)

    def refused(value: str) -> None:
        raise ValueError("unknown provider")

    agent.set_provider = refused  # type: ignore[method-assign]
    inter._handle_slash_command("/provider bad", agent)
    assert "Cannot switch provider" in out(capsys)


def test_dispatch_sessions_learn_config_clear_status(agent, capsys) -> None:
    assert inter._handle_slash_command("/sessions", agent) is True
    assert "Sessions: 3" in out(capsys)

    inter._handle_slash_command("/learn language Indonesian", agent)
    assert ("learn", ("language", "Indonesian")) in agent.calls

    inter._handle_slash_command("/learn", agent)
    assert "Usage: /learn" in out(capsys)

    inter._handle_slash_command("/config", agent)
    assert "Configuration" in out(capsys)

    inter._handle_slash_command("/clear", agent)
    assert ("clear", ()) in agent.calls

    inter._handle_slash_command("/status", agent)
    assert "AegisX Status" in out(capsys)


def test_dispatch_skills_paths(agent, capsys) -> None:
    inter._handle_slash_command("/skills", agent)
    assert "Learned Skills" in out(capsys)

    inter._handle_slash_command("/skills deploy", agent)
    assert "Closest matches" in out(capsys)

    agent.search_skills = lambda query: []  # type: ignore[method-assign]
    inter._handle_slash_command("/skills nothing", agent)
    assert "No skill matches" in out(capsys)

    agent.list_skills = lambda: []  # type: ignore[method-assign]
    inter._handle_slash_command("/skills", agent)
    assert "No skills learned yet" in out(capsys)


def test_dispatch_ingest_paths(agent, tmp_path, capsys, monkeypatch) -> None:
    doc = tmp_path / "doc.txt"
    doc.write_text("hello", encoding="utf-8")
    directory = tmp_path / "docs"
    directory.mkdir()

    class FakeRAG:
        async def ingest_directory(self, path: str) -> int:
            return 7

    async def fake_ingest_document(path: str) -> int:
        return 2

    agent._rag_engine = FakeRAG()
    agent.ingest_document = fake_ingest_document  # type: ignore[method-assign]

    inter._handle_slash_command(f"/ingest {directory}", agent)
    assert "Ingested 7 chunks" in out(capsys)

    inter._handle_slash_command(f"/ingest {doc}", agent)
    assert "Ingested 2 chunks" in out(capsys)

    inter._handle_slash_command("/ingest /no/such/path", agent)
    assert "Path not found" in out(capsys)

    inter._handle_slash_command("/ingest", agent)
    assert "Usage: /ingest" in out(capsys)


def test_dispatch_quit_raises_and_unknown_returns_false(agent) -> None:
    with pytest.raises(SystemExit):
        inter._handle_slash_command("/quit", agent)

    assert inter._handle_slash_command("/not-a-command", agent) is False


def test_every_delegating_command_reaches_its_handler(agent, capsys, monkeypatch) -> None:
    seen: list[str] = []
    for name, command in (
        ("_handle_code_command", "/code"),
        ("_handle_git_command", "/git"),
        ("_handle_test_command", "/test"),
        ("_handle_schedule_command", "/schedule"),
        ("_handle_plugin_command", "/plugin"),
        ("_handle_permissions_command", "/permissions"),
    ):
        monkeypatch.setattr(inter, name, lambda args, agent, _n=name: seen.append(_n))
        inter._handle_slash_command(command, agent)
    assert sorted(seen) == sorted(
        ["_handle_code_command", "_handle_git_command", "_handle_test_command",
         "_handle_schedule_command", "_handle_plugin_command", "_handle_permissions_command"]
    )


# === workspace / piped prompt ===


def test_print_workspace_silently_skips_when_no_project(agent, capsys) -> None:
    inter._print_workspace(agent)
    assert out(capsys) == ""


def test_read_piped_prompt_variants(monkeypatch) -> None:
    class FakeStdin:
        def __init__(self, is_tty: bool, data: str = "") -> None:
            self._is_tty = is_tty
            self._data = data

        def isatty(self) -> bool:
            return self._is_tty

        def read(self) -> str:
            return self._data

    monkeypatch.setattr("sys.stdin", FakeStdin(False, "  do the thing \n"))
    assert inter._read_piped_prompt() == "do the thing"

    monkeypatch.setattr("sys.stdin", FakeStdin(True, "ignored"))
    assert inter._read_piped_prompt() == ""

    class BrokenStdin:
        def isatty(self) -> bool:
            return False

        def read(self) -> str:
            raise OSError("closed")

    monkeypatch.setattr("sys.stdin", BrokenStdin())
    assert inter._read_piped_prompt() == ""


# === _run_chat loop ===


def test_run_chat_loops_slash_commands_and_exits(agent, capsys, monkeypatch) -> None:
    answers = iter(["/help", "   ", "/quit"])

    class FakePrompt:
        @staticmethod
        def ask(*args: Any, **kwargs: Any) -> str:
            return next(answers)

    monkeypatch.setattr(inter, "Prompt", FakePrompt)
    inter._run_chat(agent)

    captured = out(capsys)
    assert "AEGISX AGENT" in captured  # banner
    assert "Commands" in captured


def test_run_chat_handles_keyboard_interrupt_and_value_error(agent, capsys, monkeypatch) -> None:
    answers = ["fix the bug", "/quit"]

    class FakePrompt:
        index = 0

        @staticmethod
        def ask(*args: Any, **kwargs: Any) -> str:
            value = answers[FakePrompt.index]
            FakePrompt.index += 1
            return value

    def chat_with_value_error(agent_arg, message: str, no_stream: bool = False) -> None:
        raise ValueError("set your API key")

    monkeypatch.setattr(inter, "Prompt", FakePrompt)
    monkeypatch.setattr(inter, "_chat_with_animation", chat_with_value_error)
    inter._run_chat(agent)

    captured = out(capsys)
    assert "Setup Required" in captured

    def interrupt(agent_arg, message: str, no_stream: bool = False) -> None:
        raise KeyboardInterrupt

    FakePrompt.index = 0
    monkeypatch.setattr(inter, "_chat_with_animation", interrupt)
    inter._run_chat(agent)
    assert "Interrupted" in out(capsys)


def test_run_chat_classifies_connection_and_auth_errors(agent, capsys, monkeypatch) -> None:
    def auth_error(agent_arg, message: str, no_stream: bool = False) -> None:
        raise RuntimeError("401 Unauthorized Bearer bad")

    def connect_error(agent_arg, message: str, no_stream: bool = False) -> None:
        raise RuntimeError("connection timeout to provider")

    class FakePrompt:
        index = 0

        @staticmethod
        def ask(*args: Any, **kwargs: Any) -> str:
            # One chat message first so the loop reaches the failing handler,
            # then quit.
            value = "do a thing" if FakePrompt.index == 0 else "/quit"
            FakePrompt.index += 1
            return value

    monkeypatch.setattr(inter, "Prompt", FakePrompt)
    for failing in (auth_error, connect_error):
        FakePrompt.index = 0
        monkeypatch.setattr(inter, "_chat_with_animation", failing)
        inter._run_chat(agent)
    captured = out(capsys)
    assert "Authentication Failed" in captured
    assert "Connection Error" in captured


def test_chat_with_animation_streams_and_panels(agent, capsys, monkeypatch) -> None:
    async def fake_stream(message: str):
        for chunk in ("hel", "lo"):
            yield chunk

    agent.chat_stream = fake_stream  # type: ignore[method-assign]
    inter._chat_with_animation(agent, "hi")
    assert "hello" in out(capsys)

    async def fake_chat(message: str) -> str:
        return "full answer"

    agent.chat = fake_chat  # type: ignore[method-assign]
    inter._chat_with_animation(agent, "hi", no_stream=True)
    assert "full answer" in out(capsys)
