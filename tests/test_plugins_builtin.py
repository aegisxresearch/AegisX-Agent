"""Built-in plugins: browser, GitHub, and database — fully offline.

Network behavior is driven by the shared fake HTTP server; the database tools
run against a real temporary SQLite file. What is proven: the load path, the
manifests, the guardrails (SSRF, read-only SQL, single statement), and the
actual handlers end to end.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest
from fake_llm import FakeLLMServer

from aegisx_agent.plugins import PluginManifest, PluginRegistry
from aegisx_agent.plugins.builtin import all as builtin_all
from aegisx_agent.plugins.builtin import browser, database, github


@pytest.fixture()
def fake_http(monkeypatch):
    """Fake HTTP server with the SSRF guard relaxed so loopback is reachable."""
    monkeypatch.setenv("AEGISX_ALLOW_PRIVATE_HTTP", "1")
    server = FakeLLMServer()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture()
def sqlite_file(tmp_path):
    path = tmp_path / "shop.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE items (name TEXT, price REAL)")
        connection.executemany(
            "INSERT INTO items VALUES (?, ?)", [("mug", 3.5), ("tea", 2.0), ("cup", 4.25)]
        )
        connection.commit()
    finally:
        connection.close()
    return path


# === registry / load path ===


def test_every_builtin_loads_through_the_standard_registry() -> None:
    registry = PluginRegistry()
    loaded = registry.load_module(builtin_all)

    names = [definition.manifest.qualified_tool_name for definition in loaded]
    assert names == [
        "plugin_browser_read_page",
        "plugin_browser_http_get",
        "plugin_database_sql_query",
        "plugin_database_list_tables",
        "plugin_github_repo_info",
        "plugin_github_list_issues",
    ]
    # Mirror the real flow: load returns definitions, register_plugin installs them.
    captured: list[Any] = []

    class FakeRegistry:
        @staticmethod
        def register(tool: Any) -> None:
            captured.append(tool.name)

    for definition in loaded:
        registry.register(definition)
    registry.install_into(FakeRegistry())
    assert set(captured) == set(names)


def test_every_builtin_declares_object_schema_and_risk() -> None:
    for definition in builtin_all.PLUGINS:
        manifest = definition.manifest
        manifest.validate()  # must not raise
        assert manifest.parameters["type"] == "object"
        # A string risk must have been coerced to ToolRisk, or the gate crashes.
        assert manifest.risk.value in ("safe", "caution")


def test_string_risk_is_coerced_to_toolrisk_for_the_gate() -> None:
    """Regression: risk="safe" crashed the gate with AttributeError on first use."""
    from aegisx_agent.tools.base import ToolRisk

    for definition in builtin_all.PLUGINS:
        assert isinstance(definition.manifest.risk, ToolRisk)

    manifest = PluginManifest(
        plugin_id="coerce",
        version="1.0.0",
        tool_name="run",
        description="Coercion check.",
        risk="caution",
    )
    assert manifest.risk is ToolRisk.CAUTION

    with pytest.raises(ValueError):  # an invalid spelling is still rejected
        PluginManifest(
            plugin_id="coerce2",
            version="1.0.0",
            tool_name="run",
            description="Bad risk.",
            risk="nuclear",
        )


# === browser ===


def test_read_page_strips_html_to_text(fake_http: FakeLLMServer) -> None:
    fake_http.serve_get(
        "/docs",
        {
            "raw": "<html><head><style>x{}</style></head><body><h1>Hello</h1>"
            "<p>World &amp; more</p></body></html>",
            "content_type": "text/html",
        },
    )
    fake_http.serve_get("/plain", {"raw": "just text"})
    base = fake_http.root_url

    read_page = browser.PLUGINS[0].handler
    html = read_page(f"{base}/docs")
    assert "Hello" in html and "World & more" in html
    assert "<" not in html and "style" not in html.replace("style", "")  # tags gone

    plain = read_page(f"{base}/plain")
    assert plain == "just text"


def test_read_page_truncates_long_pages(fake_http: FakeLLMServer) -> None:
    fake_http.serve_get("/big", {"raw": "x" * 20_000})

    result = browser.PLUGINS[0].handler(f"{fake_http.root_url}/big")

    assert "truncated at 8000" in result


def test_read_page_reports_http_errors_as_value_errors(fake_http: FakeLLMServer) -> None:
    with pytest.raises(ValueError, match="fetch failed"):
        browser.PLUGINS[0].handler(f"{fake_http.root_url}/missing")  # 404 from the fake server


def test_read_page_refuses_local_and_private_targets() -> None:
    handler = browser.PLUGINS[0].handler
    for url in (
        "http://localhost:8080/x",
        "http://127.0.0.1/x",
        "http://10.0.0.1/x",
        "http://192.168.1.1/x",
        "http://host.internal/x",
        "file:///etc/passwd",
        "ftp://example.com/x",
    ):
        with pytest.raises(ValueError):
            handler(url)


def test_http_get_returns_pretty_json_and_raw_text(fake_http: FakeLLMServer) -> None:
    fake_http.serve_get("/api", {"body": {"ok": True, "n": 2}})
    fake_http.serve_get("/raw", {"raw": "RAW-BODY"})

    http_get = browser.PLUGINS[1].handler
    pretty = http_get(f"{fake_http.root_url}/api")
    assert json.loads(pretty) == {"ok": True, "n": 2}
    assert "\n" in pretty  # indented

    raw = http_get(f"{fake_http.root_url}/raw")
    assert raw == "RAW-BODY"


# === github ===


def test_github_repo_info_parses_the_api_response(fake_http: FakeLLMServer, monkeypatch) -> None:
    monkeypatch.setenv("AEGISX_GITHUB_TOKEN", "token-for-test")
    fake_http.serve_get(
        "/repos/aegisxresearch/AegisX-Agent",
        {
            "body": {
                "full_name": "aegisxresearch/AegisX-Agent",
                "stargazers_count": 42,
                "forks_count": 7,
                "open_issues_count": 3,
                "language": "Python",
                "license": {"spdx_id": "MIT"},
                "description": "Agentic AI",
            }
        },
    )
    monkeypatch.setattr(github, "API_ROOT", fake_http.root_url)
    repo_info = github.PLUGINS[0].handler

    result = repo_info("https://github.com/aegisxresearch/AegisX-Agent")

    assert "⭐ 42" in result
    assert "aegisxresearch/AegisX-Agent" in result
    assert "license MIT" in result
    headers = {k.lower(): v for k, v in fake_http.headers_seen[0].items()}
    assert headers["authorization"] == "Bearer token-for-test"


def test_github_list_issues_skips_pull_requests(fake_http: FakeLLMServer, monkeypatch) -> None:
    # The fake server matches on the full path, query string included.
    fake_http.serve_get(
        "/repos/owner/name/issues?state=open&per_page=5",
        {
            "body": [
                {"number": 2, "title": "real issue"},
                {"number": 3, "title": "a PR", "pull_request": {"url": "x"}},
            ]
        },
    )
    monkeypatch.setattr(github, "API_ROOT", fake_http.root_url)
    list_issues = github.PLUGINS[1].handler

    result = list_issues("owner/name", limit=5)
    assert "#2 — real issue" in result
    assert "a PR" not in result


def test_github_handlers_report_missing_repos(fake_http: FakeLLMServer, monkeypatch) -> None:
    monkeypatch.setattr(github, "API_ROOT", fake_http.root_url)
    repo_info = github.PLUGINS[0].handler
    fake_http.serve_get("/repos/ghost/none", {"status": 404, "body": {"message": "NF"}})

    with pytest.raises(ValueError, match="not found on GitHub"):
        repo_info("ghost/none")


def test_github_slug_validation_rejects_garbage() -> None:
    for bad in ("", "justname", "a/b/c", "https://gitlab.com/a/b"):
        with pytest.raises(ValueError):
            github._repo_slug(bad)


# === database ===


def test_sql_query_runs_selects_against_sqlite(sqlite_file) -> None:
    sql_query = database.PLUGINS[0].handler
    result = sql_query("SELECT name, price FROM items ORDER BY price", str(sqlite_file))

    assert "name" in result and "price" in result
    assert "tea" in result and "mug" in result


def test_sql_query_truncates_at_the_row_limit(tmp_path) -> None:
    path = tmp_path / "many.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE t (n INTEGER)")
        connection.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(80)])
        connection.commit()
    finally:
        connection.close()

    result = database.PLUGINS[0].handler("SELECT n FROM t", str(path))

    assert "row limit reached" in result


def test_sql_query_rejects_writes_multiple_statements_and_missing_files(sqlite_file) -> None:
    sql_query = database.PLUGINS[0].handler
    with pytest.raises(ValueError, match="only SELECT"):
        sql_query("DELETE FROM items", str(sqlite_file))
    with pytest.raises(ValueError, match="single statement"):
        sql_query("SELECT 1; SELECT 2", str(sqlite_file))
    with pytest.raises(ValueError, match="only SELECT"):
        sql_query("INSERT INTO items VALUES ('x', 1)", str(sqlite_file))
    with pytest.raises(ValueError, match="not found"):
        sql_query("SELECT 1", "/nonexistent/path/db.sqlite")


def test_sqlite_engine_itself_refuses_writes_through_the_readonly_uri(sqlite_file) -> None:
    """Belt and braces: mode=ro means even a smuggled write cannot happen."""
    connection = sqlite3.connect(f"file:{sqlite_file}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO items VALUES ('x', 1)")
    finally:
        connection.close()


def test_list_tables_reports_names_and_handles_empty_databases(tmp_path) -> None:
    list_tables = database.PLUGINS[1].handler
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()

    assert "No tables" in list_tables(str(empty))


def test_database_tools_require_a_configured_path(monkeypatch) -> None:
    monkeypatch.delenv("AEGISX_SQLITE_PATH", raising=False)
    sql_query, list_tables = (definition.handler for definition in database.PLUGINS)

    with pytest.raises(ValueError, match="no database"):
        sql_query("SELECT 1", "")
    with pytest.raises(ValueError, match="no database"):
        list_tables("")


# === agent integration ===


def test_builtin_plugins_execute_through_a_real_agent(tmp_path) -> None:
    from support import run

    from aegisx_agent.config import AgentConfig, LLMProvider
    from aegisx_agent.core import AegisXAgent

    agent = AegisXAgent(
        AgentConfig(
            llm_provider=LLMProvider.OLLAMA,
            data_dir=str(tmp_path),
            rag_enabled=False,
            web_search_enabled=False,
        )
    )
    names = agent.load_plugin_module("aegisx_agent.plugins.builtin.database")
    assert set(names) == {"plugin_database_sql_query", "plugin_database_list_tables"}

    path = tmp_path / "data.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE notes (body TEXT)")
        connection.execute("INSERT INTO notes VALUES ('hello plugin')")
        connection.commit()
    finally:
        connection.close()

    result = run(
        agent.tools.execute(
            "plugin_database_sql_query",
            {"query": "SELECT body FROM notes", "database": str(path)},
        )
    )
    assert result.is_success
    assert "hello plugin" in result.output

    assert agent.unload_plugin("database")
