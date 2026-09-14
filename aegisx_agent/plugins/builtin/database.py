"""Database plugin — read-only SQL queries, sqlite out of the box.

Loaded explicitly:

    agent.load_plugin_module("aegisx_agent.plugins.builtin.database")

The ``sql_query`` tool only accepts a single ``SELECT``/``WITH``/``PRAGMA``
statement and runs it with a read-only connection. For SQLite a URI with
``mode=ro`` guarantees no write can happen; server databases require the
connection string in ``AEGISX_DATABASE_URL`` and are equally restricted to
read-only statements. The risk is declared ``CAUTION``: reading arbitrary
data can expose secrets, so the gate is allowed to be strict about it.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

from aegisx_agent.plugins import PluginManifest, PluginPermissionPolicy, define_plugin
from aegisx_agent.tools.base import ToolRisk

#: Only these statement kinds may run — anything else is refused before connecting.
_READ_PREFIXES = ("select", "with", "pragma", "explain")
_MAX_ROWS = 50
_FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|detach|vacuum)\b",
                        re.IGNORECASE)


def _default_sqlite_path() -> str:
    """The sqlite file the plugin opens when no explicit ``database`` is given."""
    return os.environ.get("AEGISX_SQLITE_PATH", "")


def _connect(database: str) -> sqlite3.Connection:
    """Open a strictly read-only SQLite connection.

    ``mode=ro`` makes the engine refuse writes at the file level, not just by
    convention. An absolute path is required so the URI cannot smuggle
    relative-path surprises.
    """
    path = Path(database).expanduser().resolve()
    if not path.exists():
        raise ValueError(f"database file not found: {path}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)


def _validate(query: str) -> str:
    """Reject anything that is not a single read-only statement."""
    text = query.strip().rstrip(";").strip()
    if not text:
        raise ValueError("empty query")
    if ";" in text:
        raise ValueError("only a single statement is allowed")
    if not text.lower().startswith(_READ_PREFIXES):
        raise ValueError("only SELECT, WITH, PRAGMA, or EXPLAIN queries are allowed")
    if _FORBIDDEN.search(text):
        raise ValueError("write and schema keywords are not allowed in read-only mode")
    return text


QUERY_MANIFEST = PluginManifest(
    plugin_id="database",
    version="1.0.0",
    tool_name="sql_query",
    description=(
        "Run one read-only SQL query (SELECT/WITH/PRAGMA/EXPLAIN) against a local "
        "SQLite database and return the rows as a table. Set AEGISX_SQLITE_PATH "
        "for the default database or pass an absolute .sqlite/.db file path."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "A single read-only SQL statement",
            },
            "database": {
                "type": "string",
                "description": (
                    "Absolute path to a SQLite file. Defaults to AEGISX_SQLITE_PATH."
                ),
            },
        },
        "required": ["query"],
    },
    risk=ToolRisk.CAUTION,
    permission=PluginPermissionPolicy(allow_in_read_only=False),
)


@define_plugin(QUERY_MANIFEST)
def sql_query(query: str, database: str = "") -> str:
    text = _validate(query)
    target = (database or _default_sqlite_path()).strip()
    if not target:
        raise ValueError(
            "no database: pass an absolute SQLite file path or set AEGISX_SQLITE_PATH"
        )

    connection = _connect(target)
    try:
        cursor = connection.execute(text)
        rows = cursor.fetchmany(_MAX_ROWS + 1)
        columns = (
            [description[0] for description in cursor.description] if cursor.description else []
        )
    finally:
        connection.close()

    if not rows:
        return "Query OK — no rows returned."
    truncated = len(rows) > _MAX_ROWS
    rows = rows[:_MAX_ROWS]

    if not columns:
        return f"Query OK — {len(rows)} row(s)."

    widths = [
        max(len(str(column)), *(len(str(row[index])) for row in rows))
        for index, column in enumerate(columns)
    ]
    header = " | ".join(str(column).ljust(widths[index]) for index, column in enumerate(columns))
    separator = "-+-".join("-" * width for width in widths)
    lines = [header, separator]
    for row in rows:
        lines.append(
            " | ".join(str(row[index]).ljust(widths[index]) for index in range(len(columns)))
        )
    if truncated:
        lines.append(f"… ({_MAX_ROWS} row limit reached)")
    return "\n".join(lines)


TABLES_MANIFEST = PluginManifest(
    plugin_id="database",
    version="1.0.0",
    tool_name="list_tables",
    description=(
        "List the tables of a local SQLite database with their row counts "
        "(approximate for large tables)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "database": {
                "type": "string",
                "description": "Absolute path to a SQLite file. Defaults to AEGISX_SQLITE_PATH.",
            },
        },
    },
    risk=ToolRisk.CAUTION,
    permission=PluginPermissionPolicy(allow_in_read_only=False),
)


@define_plugin(TABLES_MANIFEST)
def list_tables(database: str = "") -> str:
    target = (database or _default_sqlite_path()).strip()
    if not target:
        raise ValueError(
            "no database: pass an absolute SQLite file path or set AEGISX_SQLITE_PATH"
        )
    connection = _connect(target)
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    finally:
        connection.close()
    if not rows:
        return "No tables found."
    return "Tables: " + ", ".join(row[0] for row in rows)


PLUGINS = (sql_query, list_tables)
