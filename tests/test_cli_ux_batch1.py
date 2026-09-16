"""Batch-1 UX: approval diff preview, grouped /help, and the turn HUD."""

from __future__ import annotations

from typing import Any

from aegisx_agent.cli import interactive as inter
from aegisx_agent.cli import main as cli_main
from aegisx_agent.security.permissions import PermissionRequest, ToolRisk

# === approval diff preview ===


def test_preview_shows_unified_diff_for_existing_files(tmp_path) -> None:
    target = tmp_path / "app.py"
    target.write_text("line1\nline2\n", encoding="utf-8")
    request = PermissionRequest(
        tool="editor",
        risk=ToolRisk.DANGEROUS,
        summary="rewrite app.py",
        arguments={"action": "write", "path": str(target), "content": "line1\nCHANGED\n"},
    )
    preview = cli_main._permission_preview(request)
    assert preview is not None
    assert "-line2" in preview
    assert "+CHANGED" in preview


def test_preview_for_a_new_file_shows_incoming_content(tmp_path) -> None:
    request = PermissionRequest(
        tool="file_ops",
        risk=ToolRisk.DANGEROUS,
        summary="create page",
        arguments={
            "action": "write",
            "path": str(tmp_path / "login.html"),
            "content": "<html>\n<body>\n</body>\n</html>\n",
        },
    )
    preview = cli_main._permission_preview(request)
    assert preview is not None
    assert "+ <html>" in preview  # every line is an addition


def test_preview_is_none_for_non_file_tools() -> None:
    request = PermissionRequest(
        tool="execute_code",
        risk=ToolRisk.DANGEROUS,
        summary="run python",
        arguments={"code": "import os"},
    )
    assert cli_main._permission_preview(request) is None


def test_preview_truncates_very_large_diffs(tmp_path) -> None:
    target = tmp_path / "big.txt"
    target.write_text("old\n", encoding="utf-8")
    request = PermissionRequest(
        tool="editor",
        risk=ToolRisk.DANGEROUS,
        summary="write big",
        arguments={
            "action": "write",
            "path": str(target),
            "content": "\n".join(f"l{i}" for i in range(200)),
        },
    )
    preview = cli_main._permission_preview(request)
    assert preview is not None
    assert "more lines" in preview


def test_permission_prompt_rings_the_bell(tmp_path, monkeypatch) -> None:
    """The approval panel pings the terminal so it is never missed."""
    import asyncio

    bells: list[bool] = []

    class BellConsole:
        def bell(self) -> None:
            bells.append(True)

        def print(self, *args: Any, **kwargs: Any) -> None:
            pass

    monkeypatch.setattr(cli_main, "console", BellConsole())

    class FakePrompt:
        @staticmethod
        def ask(*args: Any, **kwargs: Any) -> str:
            return "y"

    monkeypatch.setattr(cli_main, "Prompt", FakePrompt)
    request = PermissionRequest(
        tool="shell",
        risk=ToolRisk.DANGEROUS,
        summary="run ls",
        arguments={"command": "ls"},
    )
    allowed = asyncio.run(cli_main._permission_prompt(request))
    assert allowed is True
    assert bells == [True]


# === grouped /help ===


def test_help_menu_groups_commands(capsys) -> None:
    inter._show_command_menu()
    captured = out(capsys)
    assert "Session" in captured
    assert "Coding" in captured
    assert "/quit" in captured


def test_help_menu_filter_stays_flat(capsys) -> None:
    inter._show_command_menu("sched")
    captured = out(capsys)
    assert "/schedule" in captured
    assert "Session" not in captured  # groups disappear when filtering


# === turn HUD ===


def test_stream_can_emit_a_turn_summary(capsys) -> None:
    """emit_summary=True lands a dim turn footer after the answer."""
    from types import SimpleNamespace

    async def fake_stream(message: str, emit_summary: bool = False):
        yield "answer text"
        if emit_summary:
            yield "\n⚡ 42 tokens · 3 tool calls · 1.5s\n"

    agent = SimpleNamespace(chat_stream=fake_stream)
    inter._chat_with_animation(agent, "hi")
    captured = out(capsys)
    assert "answer text" in captured
    assert "42 tokens" in captured
    assert "3 tool calls" in captured


def out(capsys) -> str:
    captured = capsys.readouterr()
    return captured.out
