"""Bundled catalog of well-known MCP servers for `aegisx mcp search`.

A small, hand-curated list — the point is discoverability ("what can I even
connect to?"), not exhaustiveness. Every entry works with the stdio transport
the manager supports and can be added with `aegisx mcp add <id> <command>…`.
"""

from __future__ import annotations

from typing import Any

#: id -> {description, command, args, env (optional hint)}
CATALOG: dict[str, dict[str, Any]] = {
    "filesystem": {
        "description": "Read/write/search files in allowed directories",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "<allowed-dir>"],
    },
    "github": {
        "description": "Repos, issues, PRs, and files via the GitHub API",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_TOKEN": "<your-token>"},
    },
    "git": {
        "description": "Local git operations (status, diff, log, commit)",
        "command": "uvx",
        "args": ["mcp-server-git", "--repository", "<repo-path>"],
    },
    "sqlite": {
        "description": "Query a local SQLite database (read-only mode available)",
        "command": "uvx",
        "args": ["mcp-server-sqlite", "--db-path", "<database.db>"],
    },
    "fetch": {
        "description": "Fetch a URL and convert it to markdown for the model",
        "command": "uvx",
        "args": ["mcp-server-fetch"],
    },
    "memory": {
        "description": "Persistent knowledge-graph memory across sessions",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-memory"],
    },
    "sequential-thinking": {
        "description": "Structured step-by-step reasoning scaffold",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
    },
    "time": {
        "description": "Time and timezone conversions",
        "command": "uvx",
        "args": ["mcp-server-time"],
    },
}


def search_catalog(query: str = "") -> dict[str, dict[str, Any]]:
    """Catalog entries whose id or description matches ``query`` (all if empty)."""
    if not query:
        return dict(CATALOG)
    needle = query.lower()
    return {
        server_id: entry
        for server_id, entry in CATALOG.items()
        if needle in server_id.lower() or needle in str(entry.get("description", "")).lower()
    }


def add_command_hint(server_id: str) -> str:
    """The `aegisx mcp add` line a user can copy for a catalog entry."""
    entry = CATALOG.get(server_id)
    if entry is None:
        return ""
    args = " ".join(entry.get("args", []))
    return f"aegisx mcp add {server_id} {entry.get('command', '')} {args}".strip()


__all__ = ["CATALOG", "add_command_hint", "search_catalog"]
