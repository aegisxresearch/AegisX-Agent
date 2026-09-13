"""Codebase awareness tool — understand project structure and find code."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from aegisx_agent.tools.base import Tool, ToolResult, ToolStatus


class CodebaseTool(Tool):
    """Understand project structure, find files, and read code."""

    def __init__(self) -> None:
        super().__init__(
            name="codebase",
            description=(
                "Analyze a codebase: list project structure, find files by name/content, "
                "read code files, understand dependencies. Use this to navigate and "
                "understand any project before making changes."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["structure", "find", "read", "search", "deps", "summary"],
                        "description": "Action: structure (tree), find (files), read (code), search (content), deps (dependencies), summary (overview)",
                    },
                    "path": {
                        "type": "string",
                        "description": "File or directory path (default: current directory)",
                        "default": ".",
                    },
                    "query": {
                        "type": "string",
                        "description": "Search query for find/search actions",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max results (default: 50)",
                        "default": 50,
                    },
                },
                "required": ["action"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = kwargs.get("action", "structure")
        path = kwargs.get("path", ".")
        query = kwargs.get("query", "")
        max_results = kwargs.get("max_results", 50)

        try:
            match action:
                case "structure":
                    return self._show_structure(path, max_results)
                case "find":
                    return self._find_files(path, query, max_results)
                case "read":
                    return self._read_file(path)
                case "search":
                    return self._search_content(path, query, max_results)
                case "deps":
                    return self._show_deps(path)
                case "summary":
                    return self._project_summary(path)
                case _:
                    return ToolResult(status=ToolStatus.ERROR, output="", error=f"Unknown action: {action}")
        except Exception as e:
            return ToolResult(status=ToolStatus.ERROR, output="", error=str(e))

    def _show_structure(self, path: str, max_results: int) -> ToolResult:
        """Show directory tree."""
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return ToolResult(status=ToolStatus.ERROR, output="", error=f"Path not found: {path}")

        ignore_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", ".env", ".mypy_cache", ".pytest_cache", "dist", "build", ".eggs"}
        ignore_exts = {".pyc", ".pyo", ".so", ".o", ".class", ".jar"}

        lines = []
        count = 0

        def _walk(dir_path: Path, prefix: str = "", depth: int = 0) -> None:
            nonlocal count
            if depth > 4 or count >= max_results:
                return

            try:
                entries = sorted(dir_path.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
            except PermissionError:
                return

            for entry in entries:
                if count >= max_results:
                    break
                if entry.name in ignore_dirs or entry.name.startswith("."):
                    continue
                if entry.suffix in ignore_exts:
                    continue

                if entry.is_dir():
                    lines.append(f"{prefix}📁 {entry.name}/")
                    count += 1
                    _walk(entry, prefix + "  ", depth + 1)
                else:
                    size = entry.stat().st_size
                    size_str = self._format_size(size)
                    lines.append(f"{prefix}📄 {entry.name} ({size_str})")
                    count += 1

        _walk(p)

        if count >= max_results:
            lines.append(f"\n... ({max_results}+ items shown)")

        output = f"📁 Project Structure: {p.name}/\n\n" + "\n".join(lines)
        output += f"\n\nTotal: {count} items"
        return ToolResult(status=ToolStatus.SUCCESS, output=output)

    def _find_files(self, path: str, query: str, max_results: int) -> ToolResult:
        """Find files by name pattern."""
        if not query:
            return ToolResult(status=ToolStatus.ERROR, output="", error="Query required for find")

        p = Path(path).expanduser().resolve()
        ignore_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv"}

        matches = []
        for root, dirs, files in os.walk(p):
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            for f in files:
                if query.lower() in f.lower():
                    matches.append(os.path.relpath(os.path.join(root, f), p))
                    if len(matches) >= max_results:
                        break
            if len(matches) >= max_results:
                break

        if not matches:
            return ToolResult(status=ToolStatus.SUCCESS, output=f"No files found matching: {query}")

        output = f"Found {len(matches)} files matching '{query}':\n\n"
        for m in matches:
            output += f"  📄 {m}\n"
        return ToolResult(status=ToolStatus.SUCCESS, output=output)

    def _read_file(self, path: str) -> ToolResult:
        """Read a code file."""
        p = Path(path).expanduser()
        if not p.exists():
            return ToolResult(status=ToolStatus.ERROR, output="", error=f"File not found: {path}")
        if not p.is_file():
            return ToolResult(status=ToolStatus.ERROR, output="", error=f"Not a file: {path}")

        content = p.read_text(encoding="utf-8", errors="replace")
        lines = content.split("\n")

        # Add line numbers
        numbered = []
        for i, line in enumerate(lines[:500], 1):
            numbered.append(f"{i:4d} │ {line}")

        output = f"📄 {path} ({len(lines)} lines)\n\n" + "\n".join(numbered)
        if len(lines) > 500:
            output += f"\n\n... ({len(lines) - 500} more lines)"
        return ToolResult(status=ToolStatus.SUCCESS, output=output[:30_000])

    def _search_content(self, path: str, query: str, max_results: int) -> ToolResult:
        """Search for content in files."""
        if not query:
            return ToolResult(status=ToolStatus.ERROR, output="", error="Query required for search")

        p = Path(path).expanduser().resolve()
        ignore_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv"}

        matches = []
        for root, dirs, files in os.walk(p):
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            for f in files:
                filepath = os.path.join(root, f)
                try:
                    with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
                        for i, line in enumerate(fh, 1):
                            if query.lower() in line.lower():
                                rel = os.path.relpath(filepath, p)
                                matches.append(f"{rel}:{i}: {line.strip()[:100]}")
                                if len(matches) >= max_results:
                                    break
                except (PermissionError, UnicodeDecodeError):
                    pass
                if len(matches) >= max_results:
                    break
            if len(matches) >= max_results:
                break

        if not matches:
            return ToolResult(status=ToolStatus.SUCCESS, output=f"No matches for '{query}'")

        output = f"Found {len(matches)} matches for '{query}':\n\n"
        for m in matches:
            output += f"  {m}\n"
        return ToolResult(status=ToolStatus.SUCCESS, output=output[:20_000])

    def _show_deps(self, path: str) -> ToolResult:
        """Show project dependencies."""
        p = Path(path).expanduser().resolve()
        deps = []

        # Python
        for f in ["requirements.txt", "pyproject.toml", "setup.py", "Pipfile"]:
            fp = p / f
            if fp.exists():
                content = fp.read_text(encoding="utf-8", errors="replace")[:3000]
                deps.append(f"=== {f} ===\n{content}")

        # Node.js
        for f in ["package.json"]:
            fp = p / f
            if fp.exists():
                import json
                try:
                    data = json.loads(fp.read_text())
                    deps.append(f"=== package.json dependencies ===")
                    for k, v in data.get("dependencies", {}).items():
                        deps.append(f"  {k}: {v}")
                    for k, v in data.get("devDependencies", {}).items():
                        deps.append(f"  {k} (dev): {v}")
                except json.JSONDecodeError:
                    deps.append(f"=== {f} ===\n{fp.read_text()[:2000]}")

        # Go
        fp = p / "go.mod"
        if fp.exists():
            deps.append(f"=== go.mod ===\n{fp.read_text()[:2000]}")

        if not deps:
            return ToolResult(status=ToolStatus.SUCCESS, output="No dependency files found")

        return ToolResult(status=ToolStatus.SUCCESS, output="\n\n".join(deps))

    def _project_summary(self, path: str) -> ToolResult:
        """Generate project summary."""
        p = Path(path).expanduser().resolve()

        # Count files by extension
        ext_counts: dict[str, int] = {}
        total_files = 0
        total_lines = 0
        ignore_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv"}

        for root, dirs, files in os.walk(p):
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            for f in files:
                ext = Path(f).suffix or "(no ext)"
                ext_counts[ext] = ext_counts.get(ext, 0) + 1
                total_files += 1

                if ext in {".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".rb", ".php"}:
                    try:
                        with open(os.path.join(root, f), "r", encoding="utf-8", errors="replace") as fh:
                            total_lines += sum(1 for _ in fh)
                    except (PermissionError, UnicodeDecodeError):
                        pass

        # Top languages
        top = sorted(ext_counts.items(), key=lambda x: -x[1])[:10]

        output = f"📊 Project Summary: {p.name}\n\n"
        output += f"Total files: {total_files}\n"
        output += f"Code lines: {total_lines:,}\n\n"
        output += "Languages:\n"
        for ext, count in top:
            output += f"  {ext:<10} {count} files\n"

        # Check for common configs
        configs = []
        for f in ["pyproject.toml", "package.json", "go.mod", "Cargo.toml", "Makefile", "Dockerfile", ".gitignore", "README.md"]:
            if (p / f).exists():
                configs.append(f)
        if configs:
            output += f"\nConfig files: {', '.join(configs)}"

        return ToolResult(status=ToolStatus.SUCCESS, output=output)

    @staticmethod
    def _format_size(size: int) -> str:
        for unit in ["B", "KB", "MB"]:
            if size < 1024:
                return f"{size:.0f}{unit}"
            size /= 1024
        return f"{size:.0f}GB"
