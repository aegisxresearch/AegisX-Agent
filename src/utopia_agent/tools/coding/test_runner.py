"""Test runner tool — run tests and parse results."""

from __future__ import annotations

import asyncio
from typing import Any

from utopia_agent.tools.base import Tool, ToolResult, ToolStatus


class TestRunnerTool(Tool):
    """Run tests and parse results."""

    def __init__(self) -> None:
        super().__init__(
            name="run_tests",
            description=(
                "Run tests for the project. Supports pytest, unittest, npm test, "
                "go test, cargo test, and more. Automatically detects test framework. "
                "Returns test results with pass/fail counts and error details."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Custom test command (overrides auto-detection)",
                    },
                    "path": {
                        "type": "string",
                        "description": "Project directory (default: current)",
                        "default": ".",
                    },
                    "test_file": {
                        "type": "string",
                        "description": "Run specific test file",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default: 120)",
                        "default": 120,
                    },
                },
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        custom_cmd = kwargs.get("command", "")
        path = kwargs.get("path", ".")
        test_file = kwargs.get("test_file", "")
        timeout = kwargs.get("timeout", 120)

        # Determine test command
        if custom_cmd:
            cmd = custom_cmd
        else:
            cmd = self._detect_test_command(path, test_file)

        if not cmd:
            return ToolResult(
                status=ToolStatus.ERROR,
                output="",
                error="No test framework detected. Use 'command' parameter to specify.",
            )

        try:
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=path,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)

            stdout_str = stdout.decode("utf-8", errors="replace")
            stderr_str = stderr.decode("utf-8", errors="replace")
            exit_code = process.returncode

            # Parse output
            output = self._parse_output(cmd, stdout_str, stderr_str, exit_code)
            status = ToolStatus.SUCCESS if exit_code == 0 else ToolStatus.ERROR

            return ToolResult(
                status=status,
                output=output[:15_000],
                error=None if exit_code == 0 else f"Tests failed (exit code {exit_code})",
                metadata={"exit_code": exit_code, "command": cmd},
            )

        except asyncio.TimeoutError:
            return ToolResult(
                status=ToolStatus.TIMEOUT,
                output="",
                error=f"Tests timed out after {timeout}s",
            )
        except Exception as e:
            return ToolResult(status=ToolStatus.ERROR, output="", error=str(e))

    def _detect_test_command(self, path: str, test_file: str) -> str:
        """Auto-detect test framework and return command."""
        from pathlib import Path
        p = Path(path)

        test_target = f" {test_file}" if test_file else ""

        # Python
        if (p / "pyproject.toml").exists() or (p / "setup.py").exists():
            return f"cd {path} && python -m pytest{test_target} -v --tb=short"
        if list(p.glob("test_*.py")) or list(p.glob("**/test_*.py")):
            return f"cd {path} && python -m pytest{test_target} -v --tb=short"

        # Node.js
        if (p / "package.json").exists():
            return f"cd {path} && npm test"

        # Go
        if (p / "go.mod").exists():
            return f"cd {path} && go test -v ./..."

        # Rust
        if (p / "Cargo.toml").exists():
            return f"cd {path} && cargo test"

        return ""

    def _parse_output(self, cmd: str, stdout: str, stderr: str, exit_code: int) -> str:
        """Parse test output into readable format."""
        lines = []

        if exit_code == 0:
            lines.append("✅ All tests passed!")
        else:
            lines.append(f"❌ Tests failed (exit code {exit_code})")

        lines.append(f"\nCommand: {cmd}\n")

        # Show last 50 lines of output
        output = stdout if stdout else stderr
        output_lines = output.strip().split("\n")
        if len(output_lines) > 50:
            lines.append("... (truncated)\n")
            lines.extend(output_lines[-50:])
        else:
            lines.extend(output_lines)

        return "\n".join(lines)
