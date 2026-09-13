"""Shell command execution tool."""

from __future__ import annotations

import asyncio
from typing import Any

from aegisx_agent.tools.base import Tool, ToolResult, ToolRisk, ToolStatus


class ShellTool(Tool):
    """Execute shell commands."""

    def __init__(self) -> None:
        super().__init__(
            name="shell",
            description=(
                "Execute a shell command on the system. Use this for system administration, "
                "package management, running scripts, or any command-line task. "
                "Commands run in bash. Be cautious with destructive commands."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory (optional)",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default: 60)",
                        "default": 60,
                    },
                },
                "required": ["command"],
            },
            risk=ToolRisk.DANGEROUS,
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        command = kwargs.get("command", "")
        cwd = kwargs.get("cwd")
        timeout = kwargs.get("timeout", 60)

        if not command.strip():
            return ToolResult(status=ToolStatus.ERROR, output="", error="No command provided")

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )

            stdout_str = stdout.decode("utf-8", errors="replace").strip()
            stderr_str = stderr.decode("utf-8", errors="replace").strip()
            exit_code = process.returncode

            output_parts = []
            if stdout_str:
                output_parts.append(f"STDOUT:\n{stdout_str}")
            if stderr_str:
                output_parts.append(f"STDERR:\n{stderr_str}")
            output_parts.append(f"Exit code: {exit_code}")

            result = "\n\n".join(output_parts)
            status = ToolStatus.SUCCESS if exit_code == 0 else ToolStatus.ERROR
            error = None if exit_code == 0 else f"Command exited with code {exit_code}"

            return ToolResult(
                status=status,
                output=result[:50_000],  # Truncate large outputs
                error=error,
                metadata={"exit_code": exit_code},
            )
        except asyncio.TimeoutError:
            return ToolResult(
                status=ToolStatus.TIMEOUT,
                output="",
                error=f"Command timed out after {timeout}s",
            )
        except Exception as e:
            return ToolResult(status=ToolStatus.ERROR, output="", error=str(e))
