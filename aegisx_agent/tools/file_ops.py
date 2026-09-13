"""File operations tool — read, write, list, and search files."""

from __future__ import annotations

import os
import pathlib
from typing import Any

from aegisx_agent.tools.base import Tool, ToolResult, ToolRisk, ToolStatus

#: Reading is safe, writing is recoverable, deleting is not.
_ACTION_RISKS: dict[str, ToolRisk] = {
    "read": ToolRisk.SAFE,
    "list": ToolRisk.SAFE,
    "search": ToolRisk.SAFE,
    "info": ToolRisk.SAFE,
    "write": ToolRisk.CAUTION,
    "delete": ToolRisk.DANGEROUS,
}


class FileOperationsTool(Tool):
    """Read, write, list, and search files on the local filesystem."""

    def __init__(self) -> None:
        super().__init__(
            name="file_ops",
            description=(
                "Perform file operations: read, write, list, search, or delete files. "
                "Supports reading any text file, writing new files, listing directory "
                "contents, searching for text patterns, and basic file management."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["read", "write", "list", "search", "delete", "info"],
                        "description": "The file operation to perform",
                    },
                    "path": {
                        "type": "string",
                        "description": "File or directory path",
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to write (for write action)",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Search pattern (for search action)",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "Search recursively (for search action)",
                        "default": False,
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max results for list/search (default: 50)",
                        "default": 50,
                    },
                },
                "required": ["action", "path"],
            },
            risk=ToolRisk.SAFE,
        )

    def risk_for(self, arguments: dict[str, Any]) -> ToolRisk:
        """``delete`` can remove a whole tree, so it needs approval."""
        action = str(arguments.get("action", "")).lower()
        # An unrecognised action fails in ``execute`` anyway; treating it as
        # dangerous keeps the failure on the safe side of the gate.
        return _ACTION_RISKS.get(action, ToolRisk.DANGEROUS)

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = kwargs.get("action", "")
        path = kwargs.get("path", "")

        if not path:
            return ToolResult(status=ToolStatus.ERROR, output="", error="Path is required")

        try:
            match action:
                case "read":
                    return self._read_file(path)
                case "write":
                    return self._write_file(path, kwargs.get("content", ""))
                case "list":
                    return self._list_dir(path, kwargs.get("max_results", 50))
                case "search":
                    return self._search_in_files(
                        path, kwargs.get("pattern", ""), kwargs.get("recursive", False)
                    )
                case "delete":
                    return self._delete_file(path)
                case "info":
                    return self._file_info(path)
                case _:
                    return ToolResult(
                        status=ToolStatus.ERROR, output="", error=f"Unknown action: {action}"
                    )
        except Exception as e:
            return ToolResult(status=ToolStatus.ERROR, output="", error=str(e))

    def _read_file(self, path: str) -> ToolResult:
        p = pathlib.Path(path).expanduser()
        if not p.exists():
            return ToolResult(status=ToolStatus.ERROR, output="", error=f"File not found: {path}")
        if not p.is_file():
            return ToolResult(status=ToolStatus.ERROR, output="", error=f"Not a file: {path}")

        content = p.read_text(encoding="utf-8", errors="replace")
        if len(content) > 50_000:
            content = content[:50_000] + "\n\n... [truncated, file too large]"
        return ToolResult(status=ToolStatus.SUCCESS, output=content)

    def _write_file(self, path: str, content: str) -> ToolResult:
        p = pathlib.Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return ToolResult(
            status=ToolStatus.SUCCESS,
            output=f"Successfully wrote {len(content)} bytes to {path}",
        )

    def _list_dir(self, path: str, max_results: int) -> ToolResult:
        p = pathlib.Path(path).expanduser()
        if not p.exists():
            return ToolResult(
                status=ToolStatus.ERROR,
                output="",
                error=f"Directory not found: {path}",
            )
        if not p.is_dir():
            return ToolResult(
                status=ToolStatus.ERROR, output="", error=f"Not a directory: {path}"
            )

        entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        lines = []
        for entry in entries[:max_results]:
            prefix = "📁" if entry.is_dir() else "📄"
            size = entry.stat().st_size if entry.is_file() else 0
            size_str = self._format_size(size)
            lines.append(f"{prefix} {entry.name} ({size_str})")

        if len(list(p.iterdir())) > max_results:
            lines.append(f"\n... and {len(list(p.iterdir())) - max_results} more items")

        return ToolResult(status=ToolStatus.SUCCESS, output="\n".join(lines) or "Empty directory")

    def _search_in_files(self, path: str, pattern: str, recursive: bool) -> ToolResult:
        if not pattern:
            return ToolResult(
                status=ToolStatus.ERROR, output="", error="Search pattern is required"
            )

        p = pathlib.Path(path).expanduser()
        if not p.exists():
            return ToolResult(status=ToolStatus.ERROR, output="", error=f"Path not found: {path}")

        matches: list[str] = []
        glob_pattern = "**/*" if recursive else "*"

        for file in p.glob(glob_pattern):
            if file.is_file() and file.stat().st_size < 1_000_000:
                try:
                    content = file.read_text(encoding="utf-8", errors="replace")
                    for i, line in enumerate(content.splitlines(), 1):
                        if pattern.lower() in line.lower():
                            matches.append(f"{file}:{i}: {line.strip()}")
                            if len(matches) >= 50:
                                break
                except (PermissionError, OSError):
                    pass
            if len(matches) >= 50:
                break

        if matches:
            return ToolResult(
                status=ToolStatus.SUCCESS,
                output=f"Found {len(matches)} matches for '{pattern}':\n\n" + "\n".join(matches),
            )
        return ToolResult(status=ToolStatus.SUCCESS, output=f"No matches found for '{pattern}'")

    def _delete_file(self, path: str) -> ToolResult:
        p = pathlib.Path(path).expanduser()
        if not p.exists():
            return ToolResult(status=ToolStatus.ERROR, output="", error=f"File not found: {path}")
        if p.is_dir():
            import shutil

            shutil.rmtree(p)
            return ToolResult(status=ToolStatus.SUCCESS, output=f"Deleted directory: {path}")
        p.unlink()
        return ToolResult(status=ToolStatus.SUCCESS, output=f"Deleted file: {path}")

    def _file_info(self, path: str) -> ToolResult:
        p = pathlib.Path(path).expanduser()
        if not p.exists():
            return ToolResult(status=ToolStatus.ERROR, output="", error=f"Path not found: {path}")

        stat = p.stat()
        info = (
            f"Path: {p.absolute()}\n"
            f"Type: {'Directory' if p.is_dir() else 'File'}\n"
            f"Size: {self._format_size(stat.st_size)}\n"
            f"Modified: {stat.st_mtime}\n"
            f"Readable: {os.access(p, os.R_OK)}\n"
            f"Writable: {os.access(p, os.W_OK)}"
        )
        return ToolResult(status=ToolStatus.SUCCESS, output=info)

    @staticmethod
    def _format_size(size: int) -> str:
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"
