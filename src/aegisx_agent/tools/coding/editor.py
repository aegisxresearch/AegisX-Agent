"""Multi-file editor tool — edit code across files with diff preview."""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from aegisx_agent.tools.base import Tool, ToolResult, ToolRisk, ToolStatus


class MultiFileEditorTool(Tool):
    """Edit code files with diff preview and batch operations."""

    def __init__(self) -> None:
        super().__init__(
            name="code_edit",
            description=(
                "Edit code files. Supports: single edit (find & replace), "
                "batch edit (multiple files at once), insert (add code at line), "
                "and append. Always shows a diff preview before applying changes."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["edit", "insert", "append", "batch", "create"],
                        "description": "Edit action type",
                    },
                    "path": {
                        "type": "string",
                        "description": "File path to edit",
                    },
                    "old_code": {
                        "type": "string",
                        "description": "Code to find and replace (for edit action)",
                    },
                    "new_code": {
                        "type": "string",
                        "description": "Replacement code",
                    },
                    "line": {
                        "type": "integer",
                        "description": "Line number to insert at (for insert action)",
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to write (for create/append actions)",
                    },
                    "edits": {
                        "type": "array",
                        "description": "Batch edits: [{path, old_code, new_code}]",
                        "items": {"type": "object"},
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "Show diff without applying (default: false)",
                        "default": False,
                    },
                },
                "required": ["action"],
            },
            risk=ToolRisk.CAUTION,
        )

    def risk_for(self, arguments: dict[str, Any]) -> ToolRisk:
        """A dry run writes nothing, so it never needs approval."""
        if arguments.get("dry_run"):
            return ToolRisk.SAFE
        return ToolRisk.CAUTION

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = kwargs.get("action", "edit")
        path = kwargs.get("path", "")
        dry_run = kwargs.get("dry_run", False)

        try:
            match action:
                case "edit":
                    return self._edit_file(path, kwargs.get("old_code", ""), kwargs.get("new_code", ""), dry_run)
                case "insert":
                    return self._insert_at_line(path, kwargs.get("line", 0), kwargs.get("content", ""), dry_run)
                case "append":
                    return self._append_to_file(path, kwargs.get("content", ""), dry_run)
                case "batch":
                    return self._batch_edit(kwargs.get("edits", []), dry_run)
                case "create":
                    return self._create_file(path, kwargs.get("content", ""), dry_run)
                case _:
                    return ToolResult(status=ToolStatus.ERROR, output="", error=f"Unknown action: {action}")
        except Exception as e:
            return ToolResult(status=ToolStatus.ERROR, output="", error=str(e))

    def _edit_file(self, path: str, old_code: str, new_code: str, dry_run: bool) -> ToolResult:
        """Find and replace code in a file."""
        if not path or not old_code:
            return ToolResult(status=ToolStatus.ERROR, output="", error="path and old_code required")

        p = Path(path).expanduser()
        if not p.exists():
            return ToolResult(status=ToolStatus.ERROR, output="", error=f"File not found: {path}")

        content = p.read_text(encoding="utf-8")
        if old_code not in content:
            return ToolResult(status=ToolStatus.ERROR, output="", error=f"Code not found in {path}")

        # Generate diff
        old_lines = content.split("\n")
        new_content = content.replace(old_code, new_code, 1)
        new_lines = new_content.split("\n")

        diff = list(difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"a/{path}", tofile=f"b/{path}",
            lineterm=""
        ))

        if not diff:
            return ToolResult(status=ToolStatus.SUCCESS, output="No changes needed")

        diff_text = "\n".join(diff[:50])

        if dry_run:
            return ToolResult(status=ToolStatus.SUCCESS, output=f"📝 Diff Preview:\n\n{diff_text}")

        # Apply
        p.write_text(new_content, encoding="utf-8")
        return ToolResult(
            status=ToolStatus.SUCCESS,
            output=f"✅ Edited {path}\n\nDiff:\n{diff_text}",
            metadata={"file": path, "diff_lines": len(diff)},
        )

    def _insert_at_line(self, path: str, line: int, content: str, dry_run: bool) -> ToolResult:
        """Insert code at a specific line number."""
        if not path or line < 1:
            return ToolResult(status=ToolStatus.ERROR, output="", error="path and line required")

        p = Path(path).expanduser()
        if not p.exists():
            return ToolResult(status=ToolStatus.ERROR, output="", error=f"File not found: {path}")

        lines = p.read_text(encoding="utf-8").split("\n")
        insert_lines = content.split("\n")

        # Show context
        start = max(0, line - 3)
        end = min(len(lines), line + 3)
        context_before = "\n".join(f"{i+1:4d} │ {lines[i]}" for i in range(start, min(line - 1, len(lines))))
        context_after = "\n".join(f"{i+1:4d} │ {lines[i]}" for i in range(line - 1, min(end, len(lines))))
        new_lines_display = "\n".join(f"     + {l}" for l in insert_lines)

        preview = f"📝 Insert at line {line}:\n\n{context_before}\n{new_lines_display}\n{context_after}"

        if dry_run:
            return ToolResult(status=ToolStatus.SUCCESS, output=preview)

        # Apply
        for i, new_line in enumerate(insert_lines):
            lines.insert(line - 1 + i, new_line)
        p.write_text("\n".join(lines), encoding="utf-8")
        return ToolResult(status=ToolStatus.SUCCESS, output=f"✅ Inserted {len(insert_lines)} lines at line {line}\n\n{preview}")

    def _append_to_file(self, path: str, content: str, dry_run: bool) -> ToolResult:
        """Append code to end of file."""
        if not path:
            return ToolResult(status=ToolStatus.ERROR, output="", error="path required")

        p = Path(path).expanduser()
        if not p.exists():
            return ToolResult(status=ToolStatus.ERROR, output="", error=f"File not found: {path}")

        preview = f"📝 Append to {path}:\n\n+ {content}"

        if dry_run:
            return ToolResult(status=ToolStatus.SUCCESS, output=preview)

        with open(p, "a", encoding="utf-8") as f:
            f.write("\n" + content)
        return ToolResult(status=ToolStatus.SUCCESS, output=f"✅ Appended to {path}")

    def _batch_edit(self, edits: list[dict[str, Any]], dry_run: bool) -> ToolResult:
        """Edit multiple files at once."""
        if not edits:
            return ToolResult(status=ToolStatus.ERROR, output="", error="No edits provided")

        results = []
        for i, edit in enumerate(edits):
            path = edit.get("path", "")
            old_code = edit.get("old_code", "")
            new_code = edit.get("new_code", "")
            result = self._edit_file(path, old_code, new_code, dry_run)
            status = "✅" if result.is_success else "❌"
            results.append(f"{status} {path}: {result.output[:100]}")

        output = f"📝 Batch Edit ({len(edits)} files):\n\n" + "\n".join(results)
        return ToolResult(status=ToolStatus.SUCCESS, output=output)

    def _create_file(self, path: str, content: str, dry_run: bool) -> ToolResult:
        """Create a new file."""
        if not path:
            return ToolResult(status=ToolStatus.ERROR, output="", error="path required")

        p = Path(path).expanduser()
        if p.exists() and not dry_run:
            return ToolResult(status=ToolStatus.ERROR, output="", error=f"File already exists: {path}")

        preview = f"📝 Create {path} ({len(content.splitlines())} lines)"

        if dry_run:
            return ToolResult(status=ToolStatus.SUCCESS, output=preview)

        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return ToolResult(status=ToolStatus.SUCCESS, output=f"✅ Created {path} ({len(content.splitlines())} lines)")
