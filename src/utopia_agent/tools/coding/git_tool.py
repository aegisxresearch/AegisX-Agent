"""Git integration tool — status, diff, commit, branch, log."""

from __future__ import annotations

import asyncio
from typing import Any

from utopia_agent.tools.base import Tool, ToolResult, ToolStatus


class GitTool(Tool):
    """Git operations: status, diff, commit, branch, log."""

    def __init__(self) -> None:
        super().__init__(
            name="git",
            description=(
                "Git operations: check status, view diff, commit changes, "
                "manage branches, view log, and more. Use this for version control."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["status", "diff", "commit", "log", "branch", "add", "checkout", "stash"],
                        "description": "Git action to perform",
                    },
                    "message": {
                        "type": "string",
                        "description": "Commit message (for commit action)",
                    },
                    "files": {
                        "type": "array",
                        "description": "Files to add (for add action, default: all)",
                        "items": {"type": "string"},
                    },
                    "branch": {
                        "type": "string",
                        "description": "Branch name (for branch/checkout actions)",
                    },
                    "args": {
                        "type": "string",
                        "description": "Additional arguments",
                    },
                },
                "required": ["action"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = kwargs.get("action", "status")

        try:
            match action:
                case "status":
                    return await self._run_git("status")
                case "diff":
                    return await self._run_git("diff", kwargs.get("args", ""))
                case "commit":
                    msg = kwargs.get("message", "")
                    if not msg:
                        return ToolResult(status=ToolStatus.ERROR, output="", error="Commit message required")
                    # Auto-add before commit
                    await self._run_git("add", "-A")
                    return await self._run_git("commit", f'-m "{msg}"')
                case "log":
                    return await self._run_git("log", "--oneline -20")
                case "branch":
                    branch = kwargs.get("branch", "")
                    if branch:
                        return await self._run_git("branch", branch)
                    return await self._run_git("branch", "--list")
                case "add":
                    files = kwargs.get("files", ["-A"])
                    return await self._run_git("add", " ".join(files))
                case "checkout":
                    branch = kwargs.get("branch", "")
                    if not branch:
                        return ToolResult(status=ToolStatus.ERROR, output="", error="Branch name required")
                    return await self._run_git("checkout", branch)
                case "stash":
                    return await self._run_git("stash")
                case _:
                    return ToolResult(status=ToolStatus.ERROR, output="", error=f"Unknown action: {action}")
        except Exception as e:
            return ToolResult(status=ToolStatus.ERROR, output="", error=str(e))

    async def _run_git(self, command: str, args: str = "") -> ToolResult:
        """Run a git command."""
        full_cmd = f"git {command} {args}".strip()
        process = await asyncio.create_subprocess_shell(
            full_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)

        stdout_str = stdout.decode("utf-8", errors="replace").strip()
        stderr_str = stderr.decode("utf-8", errors="replace").strip()

        output = stdout_str if stdout_str else stderr_str
        if not output:
            output = f"git {command} completed (no output)"

        status = ToolStatus.SUCCESS if process.returncode == 0 else ToolStatus.ERROR
        error = None if process.returncode == 0 else f"Exit code: {process.returncode}"

        return ToolResult(status=status, output=output, error=error)
