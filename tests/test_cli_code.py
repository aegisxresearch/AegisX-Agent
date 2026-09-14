"""CLI /code, /git, /test slash handlers against a stub agent, not a real LLM.

The handlers' contract is: take the parsed argument, run the matching tool
through ``agent.tools.execute``, and print the result. A stub agent proves
the wiring without needing a workspace git repo or a test suite to run.
"""

from __future__ import annotations

from typing import Any

import pytest

from aegisx_agent.cli.commands.code import (
    _handle_code_command,
    _handle_git_command,
    _handle_test_command,
)
from aegisx_agent.tools.base import ToolResult, ToolStatus


class StubAgent:
    """Records tool calls and returns scripted results."""

    def __init__(self, results: dict[str, ToolResult] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._results = results or {}
        self.tools = self

    async def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        if name in self._results:
            return self._results[name]
        return ToolResult(status=ToolStatus.SUCCESS, output=f"ran:{name}")


@pytest.fixture()
def stub(capsys: pytest.CaptureFixture[str]) -> StubAgent:
    return StubAgent()


def _out(capsys: pytest.CaptureFixture[str]) -> str:
    return capsys.readouterr().out


# === /code ===


def test_code_structure_and_tree_use_the_codebase_tool(stub, capsys) -> None:
    _handle_code_command("structure /tmp/x", stub)
    _handle_code_command("tree", stub)

    assert stub.calls[0] == ("codebase", {"action": "structure", "path": "/tmp/x"})
    assert stub.calls[1] == ("codebase", {"action": "structure", "path": "."})
    assert "ran:codebase" in _out(capsys)


def test_code_find_search_read_deps_and_summary(stub, capsys) -> None:
    _handle_code_command("find main", stub)
    _handle_code_command("search import", stub)
    _handle_code_command("read app.py", stub)
    _handle_code_command("deps", stub)
    _handle_code_command("summary .", stub)

    actions = [name for name, _ in stub.calls]
    assert actions == ["codebase"] * 5
    assert stub.calls[0][1]["query"] == "main"
    assert stub.calls[1][1]["query"] == "import"
    assert stub.calls[2][1] == {"action": "read", "path": "app.py"}
    assert stub.calls[3][1] == {"action": "deps", "path": "."}
    assert stub.calls[4][1] == {"action": "summary", "path": "."}
    assert _out(capsys).count("ran:codebase") == 5


def test_code_with_no_args_prints_usage(stub, capsys) -> None:
    _handle_code_command("", stub)

    assert stub.calls == []
    assert "Usage:" in _out(capsys)


def test_code_reports_tool_errors_in_output(stub, capsys) -> None:
    failing = StubAgent(
        {"codebase": ToolResult(status=ToolStatus.ERROR, output="", error="boom")}
    )

    _handle_code_command("read x.py", failing)

    assert "boom" in _out(capsys)


# === /git ===


def test_git_status_diff_log_and_branch(stub, capsys) -> None:
    _handle_git_command("", stub)  # default subcommand is status
    _handle_git_command("diff HEAD~1", stub)
    _handle_git_command("log", stub)
    _handle_git_command("branch feature", stub)
    _handle_git_command("st", stub)  # alias

    assert stub.calls[0] == ("git", {"action": "status"})
    assert stub.calls[1] == ("git", {"action": "diff", "args": "HEAD~1"})
    assert stub.calls[2] == ("git", {"action": "log"})
    assert stub.calls[3] == ("git", {"action": "branch", "branch": "feature"})
    assert stub.calls[4] == ("git", {"action": "status"})
    assert _out(capsys).count("ran:git") == 5


def test_git_commit_requires_a_message(stub, capsys) -> None:
    _handle_git_command("commit", stub)

    assert stub.calls == []
    assert "Usage: /git commit <message>" in _out(capsys)


def test_git_commit_sends_the_message(stub, capsys) -> None:
    _handle_git_command("commit fix the bug", stub)

    assert stub.calls == [("git", {"action": "commit", "message": "fix the bug"})]
    assert "ran:git" in _out(capsys)


def test_git_unknown_subcommand_prints_usage(stub, capsys) -> None:
    _handle_git_command("rebase --onto", stub)

    assert stub.calls == []
    assert "Usage:" in _out(capsys)


# === /test ===


def test_test_runs_the_test_runner_tool_with_the_given_path(stub, capsys) -> None:
    _handle_test_command("tests/", stub)

    assert stub.calls == [("run_tests", {"path": "tests/"})]
    out = _out(capsys)
    assert "Running tests in tests/" in out
    assert "✅ Tests Passed" in out


def test_test_defaults_to_the_current_directory(stub, capsys) -> None:
    _handle_test_command("", stub)

    assert stub.calls == [("run_tests", {"path": "."})]


def test_test_marks_failures_with_a_red_panel(stub, capsys) -> None:
    failing = StubAgent(
        {"run_tests": ToolResult(status=ToolStatus.ERROR, output="2 failed", error="exit 1")}
    )

    _handle_test_command("tests/", failing)

    out = _out(capsys)
    assert "❌ Tests Failed" in out
    assert "2 failed" in out


def test_test_shows_the_error_when_the_tool_produced_no_output(stub, capsys) -> None:
    failing = StubAgent(
        {"run_tests": ToolResult(status=ToolStatus.ERROR, output="", error="exit 1")}
    )

    _handle_test_command("tests/", failing)

    out = _out(capsys)
    assert "❌ Tests Failed" in out
    assert "exit 1" in out
