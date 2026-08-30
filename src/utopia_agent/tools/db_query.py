"""Database Query tool — query SQLite and PostgreSQL databases."""

from __future__ import annotations

import json
from typing import Any

from utopia_agent.tools.base import Tool, ToolResult, ToolStatus


class DatabaseQueryTool(Tool):
    """Query SQLite or PostgreSQL databases."""

    def __init__(self) -> None:
        super().__init__(
            name="db_query",
            description=(
                "Query a database. Supports SQLite (local files) and PostgreSQL. "
                "Can execute SELECT, INSERT, UPDATE, DELETE queries. "
                "Can also list tables, describe schema, and get table stats. "
                "For SQLite, provide the file path. For PostgreSQL, provide connection string."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "SQL query to execute",
                    },
                    "database": {
                        "type": "string",
                        "description": (
                            "Database connection: "
                            "SQLite file path (e.g. './data.db') or "
                            "PostgreSQL connection string (e.g. 'postgresql://user:pass@host/db')"
                        ),
                    },
                    "params": {
                        "type": "array",
                        "description": "Query parameters for prepared statements",
                        "items": {},
                    },
                    "action": {
                        "type": "string",
                        "enum": ["query", "tables", "schema", "stats"],
                        "description": "Action: query (execute SQL), tables (list tables), schema (describe table), stats (table stats)",
                        "default": "query",
                    },
                    "table": {
                        "type": "string",
                        "description": "Table name (for schema/stats actions)",
                    },
                },
                "required": ["query", "database"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        query = kwargs.get("query", "")
        database = kwargs.get("database", "")
        params = kwargs.get("params", [])
        action = kwargs.get("action", "query")
        table = kwargs.get("table", "")

        if not database:
            return ToolResult(status=ToolStatus.ERROR, output="", error="Database path/connection is required")

        try:
            if database.startswith("postgresql://") or database.startswith("postgres://"):
                return await self._query_postgres(query, database, params, action, table)
            else:
                return await self._query_sqlite(query, database, params, action, table)
        except Exception as e:
            return ToolResult(
                status=ToolStatus.ERROR, output="", error=f"Database error: {type(e).__name__}: {e}"
            )

    async def _query_sqlite(
        self, query: str, db_path: str, params: list, action: str, table: str
    ) -> ToolResult:
        """Query SQLite database."""
        import sqlite3
        from pathlib import Path

        db_file = Path(db_path).expanduser()
        db_file.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        try:
            if action == "tables":
                cursor.execute(
                    "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') ORDER BY name"
                )
                rows = cursor.fetchall()
                if not rows:
                    return ToolResult(status=ToolStatus.SUCCESS, output="No tables found")

                output = f"Tables in {db_path}:\n\n"
                output += f"{'Name':<30} {'Type':<10}\n"
                output += "-" * 40 + "\n"
                for row in rows:
                    output += f"{row['name']:<30} {row['type']:<10}\n"
                output += f"\nTotal: {len(rows)} tables/views"
                return ToolResult(status=ToolStatus.SUCCESS, output=output)

            elif action == "schema" and table:
                cursor.execute(f"PRAGMA table_info({table})")  # noqa: S608
                columns = cursor.fetchall()
                if not columns:
                    return ToolResult(
                        status=ToolStatus.ERROR, output="", error=f"Table '{table}' not found"
                    )

                cursor.execute(f"SELECT sql FROM sqlite_master WHERE name='{table}'")  # noqa: S608
                create_sql = cursor.fetchone()

                output = f"Schema for table '{table}':\n\n"
                output += f"{'Column':<25} {'Type':<20} {'NotNull':<8} {'Default':<15} {'PK':<4}\n"
                output += "-" * 75 + "\n"
                for col in columns:
                    output += (
                        f"{col['name']:<25} {col['type']:<20} "
                        f"{'YES' if col['notnull'] else 'NO':<8} "
                        f"{str(col['dflt_value'] or ''):<15} "
                        f"{'YES' if col['pk'] else 'NO':<4}\n"
                    )
                if create_sql:
                    output += f"\nCREATE SQL:\n{create_sql[0]}"
                return ToolResult(status=ToolStatus.SUCCESS, output=output)

            elif action == "stats" and table:
                cursor.execute(f"SELECT COUNT(*) as cnt FROM {table}")  # noqa: S608
                count = cursor.fetchone()["cnt"]
                output = f"Table '{table}': {count:,} rows\nDatabase file: {db_path}"
                return ToolResult(status=ToolStatus.SUCCESS, output=output)

            else:
                # Execute query (multi-statement support)
                cursor.executescript(query) if ";" in query and not params else cursor.execute(query, params if params else [])
                conn.commit()

                try:
                    rows = cursor.fetchall()
                except Exception:
                    rows = []

                if not rows:
                    return ToolResult(
                        status=ToolStatus.SUCCESS,
                        output=f"Query executed successfully.\nRows affected: {cursor.rowcount}",
                    )

                # Format results
                columns = [desc[0] for desc in cursor.description]
                output = f"Results ({len(rows)} rows):\n\n"

                # Header
                output += " | ".join(f"{c:<20}" for c in columns) + "\n"
                output += "-" * (22 * len(columns)) + "\n"

                # Rows (limit output)
                for i, row in enumerate(rows[:100]):
                    output += " | ".join(f"{str(row[c]):<20}" for c in columns) + "\n"

                if len(rows) > 100:
                    output += f"\n... ({len(rows) - 100} more rows)"

                conn.commit()
                return ToolResult(
                    status=ToolStatus.SUCCESS,
                    output=output,
                    metadata={"row_count": len(rows), "columns": columns},
                )

        finally:
            conn.close()

    async def _query_postgres(
        self, query: str, conn_str: str, params: list, action: str, table: str
    ) -> ToolResult:
        """Query PostgreSQL database."""
        try:
            import asyncpg
        except ImportError:
            return ToolResult(
                status=ToolStatus.ERROR,
                output="",
                error="asyncpg is required for PostgreSQL. Install with: pip install asyncpg",
            )

        conn = await asyncpg.connect(conn_str)
        try:
            if action == "tables":
                rows = await conn.fetch(
                    "SELECT tablename, tabletype FROM pg_tables WHERE schemaname = 'public' "
                    "UNION SELECT viewname, 'VIEW' FROM pg_views WHERE schemaname = 'public' "
                    "ORDER BY tablename"
                )
                output = f"Tables:\n\n"
                for row in rows:
                    output += f"  {row['tablename']:<30} ({row['tabletype']})\n"
                output += f"\nTotal: {len(rows)}"
                return ToolResult(status=ToolStatus.SUCCESS, output=output)

            elif action == "schema" and table:
                rows = await conn.fetch(
                    "SELECT column_name, data_type, is_nullable, column_default "
                    "FROM information_schema.columns WHERE table_name = $1 ORDER BY ordinal_position",
                    table,
                )
                output = f"Schema for '{table}':\n\n"
                for row in rows:
                    output += f"  {row['column_name']:<25} {row['data_type']:<20} nullable={row['is_nullable']}\n"
                return ToolResult(status=ToolStatus.SUCCESS, output=output)

            elif action == "stats" and table:
                row = await conn.fetchrow(f"SELECT COUNT(*) as cnt FROM {table}")  # noqa: S608
                return ToolResult(
                    status=ToolStatus.SUCCESS, output=f"Table '{table}': {row['cnt']:,} rows"
                )

            else:
                rows = await conn.fetch(query, *params)
                if not rows:
                    return ToolResult(status=ToolStatus.SUCCESS, output="Query executed. No rows returned.")

                columns = list(rows[0].keys())
                output = f"Results ({len(rows)} rows):\n\n"
                output += " | ".join(f"{c:<20}" for c in columns) + "\n"
                output += "-" * (22 * len(columns)) + "\n"
                for row in rows[:100]:
                    output += " | ".join(f"{str(row[c]):<20}" for c in columns) + "\n"
                return ToolResult(
                    status=ToolStatus.SUCCESS,
                    output=output,
                    metadata={"row_count": len(rows), "columns": columns},
                )
        finally:
            await conn.close()
