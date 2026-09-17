"""Batch B: importing skills from share links, and `mcp connect --all`."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp_compat import needs_demo_server
from support import run
from typer.testing import CliRunner

from aegisx_agent.cli import main as cli
from aegisx_agent.cli.commands import skills as skills_cmd
from aegisx_agent.cli.commands.mcp import _connect_all, _handle_mcp_command
from aegisx_agent.cli.commands.skills import (
    SkillImportError,
    _handle_skills_command,
)
from aegisx_agent.config import AgentConfig, LLMProvider
from aegisx_agent.core import AegisXAgent
from aegisx_agent.mcp.manager import MCPManager
from aegisx_agent.security.permissions import PermissionMode
from aegisx_agent.skills.manager import SkillManager, normalize_skill_url

SKILL_MD = """# Skill: Deploy Static Site

**Description:** Publish a static folder to the CDN.

**Category:** ops
**Tags:** deploy, cdn

1. Build the bundle with `npm run build`.
2. Upload `dist/` to the CDN bucket.
"""

DEMO_SERVER = str(Path(__file__).parent / "mcp_demo_server.py")


@pytest.fixture()
def runner(tmp_path, monkeypatch):
    """Isolated CLI runner: temp data dir, no saved config, no cached agent."""
    monkeypatch.setenv("AEGISX_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("AEGISX_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(cli, "_agent", None)
    return CliRunner()


def _invoke(runner: CliRunner, *args: str) -> Any:
    return runner.invoke(cli.app, list(args))


def _agent(tmp_path: Any) -> AegisXAgent:
    config = AgentConfig(
        llm_provider=LLMProvider.CUSTOM,
        custom_base_url="http://127.0.0.1:1",
        custom_api_key="test-key",
        custom_model="fake-model",
        data_dir=str(tmp_path),
        rag_enabled=False,
        permission_mode=PermissionMode.ALLOW_ALL,
    )
    return AegisXAgent(config)


# --------------------------------------------------------------------------- #
# URL handling
# --------------------------------------------------------------------------- #


def test_gist_page_url_becomes_raw_content_url() -> None:
    raw = normalize_skill_url("https://gist.github.com/firman/abc123def")

    assert raw == "https://gist.githubusercontent.com/firman/abc123def/raw"


def test_github_blob_url_becomes_raw_url() -> None:
    raw = normalize_skill_url("https://github.com/o/r/blob/main/skills/deploy.md")

    assert raw == "https://raw.githubusercontent.com/o/r/main/skills/deploy.md"


def test_plain_and_raw_urls_are_left_alone() -> None:
    for url in (
        "https://raw.githubusercontent.com/o/r/main/x.md",
        "https://example.com/skill.md",
    ):
        assert normalize_skill_url(url) == url


# --------------------------------------------------------------------------- #
# SkillManager import paths
# --------------------------------------------------------------------------- #


def test_import_skill_text_parses_markdown(tmp_path: Any) -> None:
    manager = SkillManager(tmp_path / "skills")

    skill = manager.import_skill_text(SKILL_MD, source="test")

    assert skill.name == "Deploy Static Site"
    assert skill.category == "ops"
    assert len(skill.steps) == 2
    assert (tmp_path / "skills" / "deploy_static_site.md").is_file()


def test_import_skill_text_parses_json(tmp_path: Any) -> None:
    manager = SkillManager(tmp_path / "skills")
    payload = {
        "name": "Rotate Logs",
        "description": "Trim old log files.",
        "steps": ["List logs", "Delete older than 7 days"],
    }

    skill = manager.import_skill_text(json.dumps(payload), source="test")

    assert skill.name == "Rotate Logs"
    assert manager.get("Rotate Logs") is not None


def test_import_skill_text_rejects_nameless_content(tmp_path: Any) -> None:
    manager = SkillManager(tmp_path / "skills")

    with pytest.raises(ValueError, match="no name"):
        manager.import_skill_text("just prose, no header", source="test")


def test_import_skill_url_downloads_normalized_url(tmp_path: Any) -> None:
    manager = SkillManager(tmp_path / "skills")
    seen: list[str] = []

    def fake_fetch(url: str) -> str:
        seen.append(url)
        return SKILL_MD

    skill = manager.import_skill_url("https://gist.github.com/firman/abc123", fake_fetch)

    assert seen == ["https://gist.githubusercontent.com/firman/abc123/raw"]
    assert skill.name == "Deploy Static Site"
    assert manager.get("Deploy Static Site") is not None


def test_import_skill_file_still_works(tmp_path: Any) -> None:
    source = tmp_path / "shared.md"
    source.write_text(SKILL_MD, encoding="utf-8")
    manager = SkillManager(tmp_path / "skills")

    skill = manager.import_skill(source)

    assert skill.name == "Deploy Static Site"


# --------------------------------------------------------------------------- #
# aegisx skills … (typer group)
# --------------------------------------------------------------------------- #


def test_skills_group_is_registered(runner: CliRunner) -> None:
    result = _invoke(runner, "--help")

    assert result.exit_code == 0
    assert "skills" in result.output


def test_skills_import_from_gist_url(runner: CliRunner, tmp_path, monkeypatch) -> None:
    seen: list[str] = []

    def fake_fetch(url: str) -> str:
        seen.append(url)
        return SKILL_MD

    monkeypatch.setattr(skills_cmd, "_fetch_text", fake_fetch)

    result = _invoke(runner, "skills", "import", "https://gist.github.com/u/deadbeef")

    assert result.exit_code == 0, result.output
    assert "Imported 'Deploy Static Site'" in result.output
    assert seen == ["https://gist.githubusercontent.com/u/deadbeef/raw"]
    assert (Path(tmp_path) / "skills" / "deploy_static_site.md").is_file()


def test_skills_import_reports_a_failed_download(runner: CliRunner, monkeypatch) -> None:
    def boom(url: str) -> str:
        raise SkillImportError(f"HTTP 404 fetching {url}")

    monkeypatch.setattr(skills_cmd, "_fetch_text", boom)

    result = _invoke(runner, "skills", "import", "https://gist.github.com/u/gone")

    assert result.exit_code == 1
    assert "Skill import failed" in result.output
    assert "HTTP 404" in result.output


def test_skills_list_show_and_export_roundtrip(runner: CliRunner, tmp_path) -> None:
    source = Path(tmp_path) / "shared.md"
    source.write_text(SKILL_MD, encoding="utf-8")
    imported = _invoke(runner, "skills", "import", str(source))
    assert imported.exit_code == 0, imported.output

    listed = _invoke(runner, "skills", "list")
    assert listed.exit_code == 0, listed.output
    assert "Deploy Static Site" in listed.output

    shown = _invoke(runner, "skills", "show", "Deploy Static Site")
    assert shown.exit_code == 0, shown.output
    assert "Upload `dist/`" in shown.output

    dest = Path(tmp_path) / "out.md"
    exported = _invoke(runner, "skills", "export", "Deploy Static Site", str(dest))
    assert exported.exit_code == 0, exported.output
    assert dest.is_file()


def test_skills_export_unknown_name_exits_nonzero(runner: CliRunner) -> None:
    result = _invoke(runner, "skills", "export", "ghost")

    assert result.exit_code == 1
    assert "No skill named 'ghost'" in result.output


def test_skills_search_by_url_imports_then_keyword_finds_it(
    runner: CliRunner, monkeypatch
) -> None:
    def fake_fetch(url: str) -> str:
        return SKILL_MD.replace("Deploy Static Site", "Remote Only Skill")

    monkeypatch.setattr(skills_cmd, "_fetch_text", fake_fetch)

    # A URL passed to `search` is a shortcut for import.
    fetched = _invoke(runner, "skills", "search", "https://example.com/skill.md")
    assert fetched.exit_code == 0, fetched.output
    assert "Imported 'Remote Only Skill'" in fetched.output

    found = _invoke(runner, "skills", "search", "remote")
    assert found.exit_code == 0, found.output
    assert "Remote Only Skill" in found.output

    missing = _invoke(runner, "skills", "search", "nothing-matches-this")
    assert "No skill matches" in missing.output


# --------------------------------------------------------------------------- #
# /skills slash command
# --------------------------------------------------------------------------- #


def test_slash_skills_import_from_url(tmp_path, capsys, monkeypatch) -> None:
    agent = _agent(tmp_path)
    monkeypatch.setattr(skills_cmd, "_fetch_text", lambda url: SKILL_MD)

    _handle_skills_command("import https://gist.github.com/u/feed", agent)
    out = capsys.readouterr().out

    assert "Imported 'Deploy Static Site'" in out
    assert "Publish a static folder" in out


def test_slash_skills_list_show_export_search_and_failures(
    tmp_path, capsys, monkeypatch
) -> None:
    agent = _agent(tmp_path)
    agent.skill_manager.import_skill_text(SKILL_MD, source="test")
    # An export without an explicit destination lands in the working directory,
    # so keep the test out of the repo root.
    monkeypatch.chdir(tmp_path)

    _handle_skills_command("", agent)
    assert "Deploy Static Site" in capsys.readouterr().out

    _handle_skills_command("show Deploy Static Site", agent)
    assert "Upload `dist/`" in capsys.readouterr().out

    _handle_skills_command("search deploy", agent)
    assert "Deploy Static Site" in capsys.readouterr().out

    _handle_skills_command("export Deploy Static Site", agent)
    assert "Exported" in capsys.readouterr().out
    assert (tmp_path / "deploy_static_site.md").is_file()

    _handle_skills_command("import /nope/ghost.md", agent)
    assert "Skill import failed" in capsys.readouterr().out

    _handle_skills_command("help", agent)
    assert "/skills search" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# mcp connect --all
# --------------------------------------------------------------------------- #


class _NullRegistry:
    """Stands in for the plugin/tool registries connect_all never touches."""


def test_manager_connect_all_collects_failures(tmp_path) -> None:
    manager = MCPManager(
        tmp_path, plugin_registry=_NullRegistry(), tool_registry=_NullRegistry()
    )
    manager.persist_server("good", {"command": "python", "args": ["-m", "demo"]})
    manager.persist_server("bad", {"command": "python", "args": ["-m", "nope"]})

    async def fake_connect(server_id: str, server_config: dict[str, Any]) -> list[str]:
        if server_id == "bad":
            raise OSError("executable vanished")
        return [f"mcp_{server_id}_echo"]

    manager.connect_server = fake_connect  # type: ignore[method-assign]

    connected, failures = run(manager.connect_all())

    assert connected == {"good": ["mcp_good_echo"]}
    assert "executable vanished" in failures["bad"]


def test_slash_mcp_connect_all_reports_each_server(tmp_path, capsys) -> None:
    agent = _agent(tmp_path)

    async def fake_all() -> tuple[dict[str, list[str]], dict[str, str]]:
        return {"files": ["mcp_files_read"]}, {"ghost": "MCPManagerError: boom"}

    agent.connect_all_mcp_servers = fake_all  # type: ignore[method-assign]

    _handle_mcp_command("connect --all", agent)
    out = capsys.readouterr().out

    assert "files" in out and "connected" in out
    assert "ghost" in out and "failed" in out
    assert "1 tool(s)" in out


def test_slash_mcp_connect_all_without_config(tmp_path, capsys) -> None:
    agent = _agent(tmp_path)

    _handle_mcp_command("connect --all", agent)
    out = capsys.readouterr().out

    assert "No MCP servers configured" in out


def test_connect_all_helper_reports_failure_and_returns_false(
    tmp_path, monkeypatch, capsys
) -> None:
    agent = _agent(tmp_path)

    async def fake_all() -> tuple[dict[str, list[str]], dict[str, str]]:
        return {}, {"broken": "MCPClientError: no such file"}

    monkeypatch.setattr(agent, "connect_all_mcp_servers", fake_all)

    assert _connect_all(agent) is False  # the slash helper is synchronous
    out = capsys.readouterr().out
    assert "broken" in out and "no such file" in out


@needs_demo_server
def test_mcp_connect_all_cli_mixes_healthy_and_broken(runner, tmp_path) -> None:
    """One live demo server plus one broken command, in a single pass."""
    (Path(tmp_path) / "mcp_servers.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "demo": {"command": sys.executable, "args": [DEMO_SERVER]},
                    "broken": {
                        "command": sys.executable,
                        "args": ["-c", "raise SystemExit(1)"],
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    result = _invoke(runner, "mcp", "connect", "--all")

    assert "demo" in result.output
    assert "broken" in result.output
    assert result.exit_code == 1  # one server failed, the other still connected


@needs_demo_server
def test_slash_mcp_connect_all_with_real_server(tmp_path, capsys) -> None:
    """The slash variant drives the same path against a live stdio server."""
    agent = _agent(tmp_path)
    agent.mcp.persist_server("demo", {"command": sys.executable, "args": [DEMO_SERVER]})

    _handle_mcp_command("connect --all", agent)
    out = capsys.readouterr().out

    assert "demo" in out
    assert "connected" in out
    assert agent.mcp.is_connected("demo")
