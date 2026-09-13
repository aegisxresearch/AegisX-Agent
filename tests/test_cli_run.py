"""``aegisx run`` — one task, in this folder, then exit.

This is the "just type aegisx in the project" path, so the tests cover the three
ways in: an argument, a pipe, and nothing at all.
"""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from aegisx_agent.cli import main as cli
from aegisx_agent.core import AegisXAgent
from aegisx_agent.project import ProjectContext


@pytest.fixture()
def runner(tmp_path, monkeypatch):
    """Isolated CLI runner with a deterministic workspace."""
    monkeypatch.setenv("AEGISX_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("AEGISX_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(cli, "_agent", None)
    monkeypatch.setattr(
        "aegisx_agent.core.agent.detect_project",
        lambda *args, **kwargs: ProjectContext(
            root=tmp_path,
            stacks=["Python"],
            git_branch="main",
            git_changed=2,
            git_is_repo=True,
            file_count=42,
            instructions_file="AGENTS.md",
            instructions="Always run the tests.",
        ),
    )
    return CliRunner()


@pytest.fixture()
def canned(monkeypatch):
    """Replace the agent's model call, keeping the rest of the agent real."""
    seen: dict[str, Any] = {}

    async def fake_chat(self: AegisXAgent, message: str) -> str:
        seen["message"] = message
        seen["mode"] = self.permission_gate.mode.value
        seen["tools"] = len(self.tools.list_tools())
        return f"echo: {message}"

    monkeypatch.setattr(AegisXAgent, "chat", fake_chat)
    return seen


def _invoke(runner: CliRunner, *args: str, **kwargs: Any) -> Any:
    return runner.invoke(cli.app, list(args), **kwargs)


def test_run_answers_once_and_exits(runner: CliRunner, canned) -> None:
    result = _invoke(runner, "run", "tulis test untuk scheduler")

    assert result.exit_code == 0, result.output
    assert canned["message"] == "tulis test untuk scheduler"
    assert "echo: tulis test untuk scheduler" in result.output
    assert "done in" in result.output


def test_run_shows_where_it_is_working(runner: CliRunner, canned) -> None:
    result = _invoke(runner, "run", "apa isi folder ini?")

    assert result.exit_code == 0
    assert "📁" in result.output
    assert "git main, 2 changed" in result.output
    assert "42 files" in result.output
    assert "AGENTS.md loaded" in result.output


def test_run_reads_the_task_from_stdin(runner: CliRunner, canned) -> None:
    result = _invoke(runner, "run", input="perbaiki test yang gagal\n")

    assert result.exit_code == 0, result.output
    assert canned["message"] == "perbaiki test yang gagal"


def test_run_without_a_task_fails_with_usage(runner: CliRunner, canned) -> None:
    result = _invoke(runner, "run", input="")

    assert result.exit_code == 1
    assert "No task given" in result.output
    assert "aegisx run" in result.output
    assert "message" not in canned  # the model was never called


def test_run_reports_a_failure_with_a_nonzero_exit(runner: CliRunner, monkeypatch) -> None:
    async def boom(self: AegisXAgent, message: str) -> str:
        raise RuntimeError("provider unreachable")

    monkeypatch.setattr(AegisXAgent, "chat", boom)

    result = _invoke(runner, "run", "halo")

    assert result.exit_code == 1
    assert "Task failed" in result.output
    assert "provider unreachable" in result.output


def test_run_honours_the_permission_mode_flag(runner: CliRunner, canned) -> None:
    result = _invoke(runner, "run", "halo", "--permission-mode", "read-only")

    assert result.exit_code == 0
    assert canned["mode"] == "read-only"


def test_run_defaults_to_ask_mode(runner: CliRunner, canned) -> None:
    _invoke(runner, "run", "halo")

    assert canned["mode"] == "ask"


def test_run_rejects_an_unknown_permission_mode(runner: CliRunner, canned) -> None:
    result = _invoke(runner, "run", "halo", "--permission-mode", "yolo")

    assert result.exit_code != 0
    assert "unknown permission mode" in result.output
    assert "message" not in canned


def test_run_is_listed_in_help(runner: CliRunner) -> None:
    output = _invoke(runner, "--help").output

    assert "run" in output
