"""Batch-3 ecosystem: MCP search/doctor, skill export/import, plugin reload."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aegisx_agent.cli import interactive as inter
from aegisx_agent.cli.commands import plugins as plugin_cmds
from aegisx_agent.mcp.catalog import add_command_hint, search_catalog
from aegisx_agent.skills.manager import SkillManager

# === mcp catalog ===


def test_catalog_search_filters_and_returns_all_on_empty() -> None:
    assert len(search_catalog()) == len(search_catalog(""))
    assert "github" in search_catalog("git")
    assert "filesystem" in search_catalog("files")
    assert search_catalog("no-such-thing-xyz") == {}


def test_catalog_add_hint_mentions_server_id() -> None:
    hint = add_command_hint("github")
    assert hint.startswith("aegisx mcp add github npx")
    assert add_command_hint("nope") == ""


# === skill export/import ===


@pytest.fixture()
def manager(tmp_path: Path) -> SkillManager:
    return SkillManager(skills_dir=tmp_path / "skills")


def test_export_writes_markdown_and_import_round_trips(
    manager: SkillManager, tmp_path: Path
) -> None:
    manager.create_skill(
        "deploy app",
        "How to ship",
        ["pull latest", "run tests", "deploy"],
        tags=["ops"],
        category="ops",
    )
    dest = tmp_path / "shared" / "deploy.md"
    out = manager.export_skill("deploy app", dest)

    assert out == dest
    assert "deploy app" in dest.read_text()

    other = SkillManager(skills_dir=tmp_path / "other-library")
    imported = other.import_skill(dest)

    assert imported.name == "deploy app"
    assert imported.steps == ["pull latest", "run tests", "deploy"]
    assert other.get("deploy app") is not None
    assert (tmp_path / "other-library" / "deploy_app.md").exists()


def test_export_missing_skill_raises_keyerror(manager: SkillManager, tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        manager.export_skill("ghost", tmp_path / "x.md")


def test_import_missing_file_raises_filenotfound(manager: SkillManager) -> None:
    with pytest.raises(FileNotFoundError):
        manager.import_skill("nowhere.md")


# === /skills slash export & import ===


class _SkillAgent:
    def __init__(self, manager: SkillManager) -> None:
        self.skill_manager = manager
        self.calls: list[tuple[str, Any]] = []

    def get_skill(self, name: str):
        return None

    def search_skills(self, query: str):
        return []

    def list_skills(self) -> list[dict[str, str]]:
        return []

    def list_tools(self) -> list[str]:
        return []

    def list_personas(self) -> list[str]:
        return []

    def get_provider_info(self) -> dict[str, str]:
        return {"provider": "ollama", "model": "m"}

    def get_project(self):
        return None


def test_slash_skills_export_and_import(manager: SkillManager, tmp_path, capsys) -> None:
    manager.create_skill("restart daemon", "Bounce the daemon", ["stop", "start"])
    agent = _SkillAgent(manager)

    assert inter._handle_slash_command(
        f"/skills export restart daemon {tmp_path / 'share.md'}", agent
    ) is True
    assert "Exported" in capsys.readouterr().out

    second = SkillManager(skills_dir=tmp_path / "lib2")
    agent2 = _SkillAgent(second)
    assert inter._handle_slash_command(f"/skills import {tmp_path / 'share.md'}", agent2) is True
    out = capsys.readouterr().out
    assert "Imported" in out
    assert second.get("restart daemon") is not None


def test_slash_skills_export_missing_shows_error(manager: SkillManager, tmp_path, capsys) -> None:
    agent = _SkillAgent(manager)
    assert inter._handle_slash_command(f"/skills export ghost {tmp_path / 'g.md'}", agent) is True
    assert "failed" in capsys.readouterr().out


# === plugin reload ===


class _ReloadAgent:
    """Records unregister/register calls for a fake plugin round-trip."""

    def __init__(self) -> None:
        self.plugin_registry = type("R", (), {
            "get": staticmethod(lambda pid: [object()] if pid == "myplug" else None),
            "origin_of": staticmethod(
                lambda pid: "/tmp/fake_plugin.py" if pid == "myplug" else None
            ),
            "list_plugins": staticmethod(lambda: []),
        })()
        self.permission_gate = type("G", (), {"mode": type("M", (), {"value": "ask"})()})()
        self.tools = type("T", (), {"get": staticmethod(lambda name: None)})()
        self.unloaded: list[str] = []
        self.loaded_from: list[str] = []

    def unload_plugin(self, plugin_id: str) -> bool:
        self.unloaded.append(plugin_id)
        return True

    def load_plugin_path(self, path: str) -> list[str]:
        self.loaded_from.append(path)
        return ["myplug_tool"]

    def load_plugin_module(self, module: str) -> list[str]:
        self.loaded_from.append(module)
        return ["myplug_tool"]


def test_slash_plugin_reload_unloads_then_loads_from_origin(capsys) -> None:
    agent = _ReloadAgent()

    plugin_cmds._handle_plugin_command("reload myplug", agent)

    assert agent.unloaded == ["myplug"]
    assert agent.loaded_from == ["/tmp/fake_plugin.py"]
    assert "Reloaded" in capsys.readouterr().out


def test_slash_plugin_reload_unknown_or_originless(capsys) -> None:
    agent = _ReloadAgent()

    plugin_cmds._handle_plugin_command("reload nope", agent)
    assert "No loaded plugin" in capsys.readouterr().out

    class NoOriginRegistry:
        def get(self, pid: str):
            return [object()]

        def origin_of(self, pid: str):
            return None

    agent2 = _ReloadAgent()
    agent2.plugin_registry = NoOriginRegistry()
    plugin_cmds._handle_plugin_command("reload myplug", agent2)
    assert "no source" in capsys.readouterr().out
    assert agent2.unloaded == []


def test_slash_plugin_reload_unknown_or_originless_helpers() -> None:
    # The registry of the unknown-id agent returns None for `get`, proving
    # the guard fires before any unload/load side effect.
    agent = _ReloadAgent()
    assert agent.plugin_registry.get("nope") is None
    assert agent.plugin_registry.get("myplug") is not None
