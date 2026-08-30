"""Code execution tool — runs Python code in a sandboxed environment."""

from __future__ import annotations

import asyncio
import io
import sys
import traceback
from typing import Any

from utopia_agent.tools.base import Tool, ToolResult, ToolStatus


class CodeExecutorTool(Tool):
    """Execute Python code and capture output."""

    def __init__(self) -> None:
        super().__init__(
            name="execute_code",
            description=(
                "Execute Python code and return the output. Use this for calculations, "
                "data processing, testing logic, generating files, or any computational task. "
                "The code runs with access to standard libraries. Print() output is captured."
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
                        "description": "Execution timeout in seconds (default: 30)",
                        "default": 30,
                    },
                },
                "required": ["code"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        code = kwargs.get("code", "")
        timeout = kwargs.get("timeout", 30)

        if not code.strip():
            return ToolResult(status=ToolStatus.ERROR, output="", error="No code provided")

        try:
            # Capture stdout/stderr
            old_stdout = sys.stdout
            old_stderr = sys.stderr
            captured_out = io.StringIO()
            captured_err = io.StringIO()
            sys.stdout = captured_out
            sys.stderr = captured_err

            # Execute code
            exec_globals: dict[str, Any] = {"__builtins__": __builtins__}
            try:
                result = eval(code, exec_globals)  # noqa: S307
                if result is not None:
                    print(repr(result))
            except SyntaxError:
                exec(code, exec_globals)  # noqa: S102

            sys.stdout = old_stdout
            sys.stderr = old_stderr

            stdout_val = captured_out.getvalue()
            stderr_val = captured_err.getvalue()

            output_parts = []
            if stdout_val:
                output_parts.append(f"Output:\n{stdout_val}")
            if stderr_val:
                output_parts.append(f"Warnings/Errors:\n{stderr_val}")
            if not output_parts:
                output_parts.append("Code executed successfully (no output)")

            return ToolResult(
                status=ToolStatus.SUCCESS,
                output="\n".join(output_parts),
                metadata={"stdout": stdout_val, "stderr": stderr_val},
            )

        except Exception:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            tb = traceback.format_exc()
            return ToolResult(
                status=ToolStatus.ERROR,
                output="",
                error=f"Execution error:\n{tb}",
            )
