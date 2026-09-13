"""Code execution tool — runs Python in this process.

There is **no sandbox here**. The code is handed to ``eval``/``exec`` with the
real ``__builtins__``, so it can read and write files, import anything, and
reach the network with the same rights as the agent process.

That is a deliberate trade-off (a real sandbox needs OS-level isolation this
project does not ship), and it is why the tool declares
:attr:`ToolRisk.DANGEROUS`: the permission gate makes every call require
approval unless the operator allow-lists ``execute_code``.
"""

from __future__ import annotations

import asyncio
import io
import sys
import traceback
from typing import Any

from aegisx_agent.tools.base import Tool, ToolResult, ToolRisk, ToolStatus

#: ``sys.stdout``/``sys.stderr`` are process-global, so two overlapping calls
#: would capture each other's output. The agent loop runs tools in parallel, so
#: execution is serialised instead.
_EXECUTION_LOCK = asyncio.Lock()


class CodeExecutorTool(Tool):
    """Execute Python code in this process and capture its output."""

    def __init__(self) -> None:
        super().__init__(
            name="execute_code",
            description=(
                "Execute Python code and return the output. Use this for calculations, "
                "data processing, testing logic, generating files, or any computational task. "
                "The code runs in the agent's own process with full access to the "
                "filesystem, the network, and installed packages — not in a sandbox — "
                "so it requires approval. Print() output is captured."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Python code to execute",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": (
                            "Seconds to wait for the code (default: 30). The agent stops "
                            "waiting when this elapses; the code itself is not killed."
                        ),
                        "default": 30,
                    },
                },
                "required": ["code"],
            },
            risk=ToolRisk.DANGEROUS,
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        code = kwargs.get("code", "")
        try:
            timeout = float(kwargs.get("timeout", 30))
        except (TypeError, ValueError):
            return ToolResult(
                status=ToolStatus.ERROR,
                output="",
                error=f"Invalid timeout: {kwargs.get('timeout')!r}",
            )

        if not code.strip():
            return ToolResult(status=ToolStatus.ERROR, output="", error="No code provided")

        async with _EXECUTION_LOCK:
            try:
                # Run off the event loop so a long computation cannot freeze the
                # agent (or a scheduler tick) while it waits.
                return await asyncio.wait_for(
                    asyncio.to_thread(self._run, code), timeout=timeout
                )
            except asyncio.TimeoutError:
                return ToolResult(
                    status=ToolStatus.TIMEOUT,
                    output="",
                    error=(
                        f"Code still running after {timeout:g}s. The agent stopped waiting, "
                        "but the code was not interrupted and may still be running."
                    ),
                )

    @staticmethod
    def _run(code: str) -> ToolResult:
        """Execute ``code`` with stdout/stderr captured, restoring them always."""
        captured_out = io.StringIO()
        captured_err = io.StringIO()
        original_stdout, original_stderr = sys.stdout, sys.stderr

        try:
            sys.stdout, sys.stderr = captured_out, captured_err
            scope: dict[str, Any] = {"__builtins__": __builtins__}
            try:
                result = eval(code, scope)  # noqa: S307 - execution is the entire point
                if result is not None:
                    print(repr(result))
            except SyntaxError:
                exec(code, scope)  # noqa: S102 - execution is the entire point
        except (KeyboardInterrupt, asyncio.CancelledError):
            # The operator interrupting is not a bug in the snippet.
            raise
        except BaseException:
            # SystemExit included: ``exit()`` in a snippet must be reported, not
            # allowed to tear down the agent that is running it.
            return ToolResult(
                status=ToolStatus.ERROR,
                output="",
                error=f"Execution error:\n{traceback.format_exc()}",
            )
        finally:
            sys.stdout, sys.stderr = original_stdout, original_stderr

        stdout_val = captured_out.getvalue()
        stderr_val = captured_err.getvalue()

        parts = []
        if stdout_val:
            parts.append(f"Output:\n{stdout_val}")
        if stderr_val:
            parts.append(f"Warnings/Errors:\n{stderr_val}")
        if not parts:
            parts.append("Code executed successfully (no output)")

        return ToolResult(
            status=ToolStatus.SUCCESS,
            output="\n".join(parts),
            metadata={"stdout": stdout_val, "stderr": stderr_val},
        )
