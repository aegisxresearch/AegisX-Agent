"""Code execution: output capture, stdout hygiene, and the enforced timeout.

``sys.stdout`` is process-global, so a tool that swaps it is one exception away
from redirecting every later print in the process. These tests pin that down,
along with the ``timeout`` the schema advertises (which used to be ignored).
"""

from __future__ import annotations

import asyncio
import sys

from support import run

from aegisx_agent.tools.base import ToolStatus
from aegisx_agent.tools.code_executor import CodeExecutorTool


def _tool() -> CodeExecutorTool:
    return CodeExecutorTool()


def test_print_output_is_captured() -> None:
    result = run(_tool().execute(code="print(6 * 7)"))

    assert result.is_success is True
    assert "42" in result.output
    assert result.metadata["stdout"].strip() == "42"


def test_a_bare_expression_is_evaluated() -> None:
    result = run(_tool().execute(code="6 * 7"))

    assert result.is_success is True
    assert "42" in result.output


def test_statements_are_executed() -> None:
    snippet = "\n".join(["total = 0", "for i in range(5):", "    total += i", "print(total)"])
    result = run(_tool().execute(code=snippet))

    assert result.is_success is True
    assert "10" in result.output


def test_stderr_is_reported_separately() -> None:
    result = run(_tool().execute(code="import sys; print('out'); print('err', file=sys.stderr)"))

    assert "out" in result.output
    assert "err" in result.output


def test_a_raising_snippet_is_an_error_result_not_an_exception() -> None:
    result = run(_tool().execute(code="raise ValueError('boom')"))

    assert result.status is ToolStatus.ERROR
    assert "boom" in (result.error or "")


def test_empty_code_is_rejected() -> None:
    result = run(_tool().execute(code="   \n  "))

    assert result.status is ToolStatus.ERROR
    assert "No code provided" in (result.error or "")


def test_a_nonsense_timeout_is_rejected() -> None:
    result = run(_tool().execute(code="1 + 1", timeout="soon"))

    assert result.status is ToolStatus.ERROR
    assert "Invalid timeout" in (result.error or "")


def test_stdout_is_restored_after_success() -> None:
    original = sys.stdout

    run(_tool().execute(code="print('hi')"))

    assert sys.stdout is original


def test_a_snippet_cannot_terminate_the_agent() -> None:
    """``exit()`` in a snippet must fail the call, not the process."""
    result = run(_tool().execute(code="import sys; sys.exit(2)"))

    assert result.status is ToolStatus.ERROR
    assert "SystemExit" in (result.error or "")


def test_stdout_is_restored_after_a_crash() -> None:
    original = sys.stdout

    run(_tool().execute(code="raise SystemExit(2)"))

    assert sys.stdout is original


def test_the_advertised_timeout_is_enforced() -> None:
    """Long code must not block the agent forever; the schema promises a timeout."""
    result = run(_tool().execute(code="import time; time.sleep(0.4)", timeout=0.05))

    assert result.status is ToolStatus.TIMEOUT
    assert "not interrupted" in (result.error or "")


def test_concurrent_calls_do_not_steal_each_other_s_output() -> None:
    """The agent loop runs tools in parallel, and stdout is process-global."""
    tool = _tool()

    async def main():
        slow = tool.execute(code="import time; time.sleep(0.05); print('SLOW')")
        fast = tool.execute(code="print('FAST')")
        return await asyncio.gather(slow, fast)

    slow_result, fast_result = run(main())

    assert slow_result.is_success and fast_result.is_success
    assert slow_result.output.strip() == "Output:\nSLOW"
    assert fast_result.output.strip() == "Output:\nFAST"


def test_execution_does_not_block_the_event_loop() -> None:
    """A CPU-bound snippet runs in a worker thread, so timers keep firing."""
    tool = _tool()
    ticks = 0

    async def main():
        nonlocal ticks

        async def tick() -> None:
            nonlocal ticks
            for _ in range(5):
                await asyncio.sleep(0.005)
                ticks += 1

        await asyncio.gather(tool.execute(code="sum(range(10_000_000))"), tick())
        return ticks

    assert run(main()) >= 5
