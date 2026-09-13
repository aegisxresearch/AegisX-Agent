"""ShellTool against real subprocesses.

Real bash, real exit codes, real timeouts — nothing is faked here. A
dangerous tool is exactly the one that must be tested end to end offline.
"""

from __future__ import annotations

from support import run

from aegisx_agent.tools.base import ToolStatus
from aegisx_agent.tools.shell import ShellTool


def test_a_successful_command_returns_stdout_and_exit_zero() -> None:
    result = run(ShellTool().execute(command="echo hello-world"))

    assert result.status is ToolStatus.SUCCESS
    assert "hello-world" in result.output
    assert "STDOUT" in result.output
    assert "Exit code: 0" in result.output
    assert result.metadata["exit_code"] == 0
    assert result.is_success


def test_stderr_is_captured_and_marks_the_command_failed() -> None:
    result = run(ShellTool().execute(command="echo oops >&2; exit 3"))

    assert result.status is ToolStatus.ERROR
    assert "STDERR" in result.output
    assert "oops" in result.output
    assert "Exit code: 3" in result.output
    assert result.metadata["exit_code"] == 3
    assert "exited with code 3" in (result.error or "")


def test_an_empty_command_is_rejected_before_any_process_starts() -> None:
    result = run(ShellTool().execute(command="   "))

    assert result.status is ToolStatus.ERROR
    assert "No command provided" in (result.error or "")


def test_cwd_controls_where_the_command_runs() -> None:
    result = run(ShellTool().execute(command="pwd", cwd="/tmp"))

    assert result.status is ToolStatus.SUCCESS
    assert "/tmp" in result.output


def test_a_timing_out_command_is_reported_as_timeout() -> None:
    result = run(ShellTool().execute(command="sleep 5", timeout=1))

    assert result.status is ToolStatus.TIMEOUT
    assert "timed out after 1s" in (result.error or "")


def test_a_crashing_shell_still_returns_a_structured_error() -> None:
    result = run(ShellTool().execute(command="exit 7"))

    assert result.status is ToolStatus.ERROR
    assert result.metadata["exit_code"] == 7


def test_large_output_is_truncated_to_the_cap() -> None:
    result = run(ShellTool().execute(command="yes aegisx | head -c 200000"))

    assert result.status is ToolStatus.SUCCESS
    assert len(result.output) <= 50_000
