"""CLI surface for plugin management: list/load/unload with gate verdicts."""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest
from typer.testing import CliRunner

from aegisx_agent.cli import main as cli
from aegisx_agent.core import AegisXAgent


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


PLUGIN_SOURCE = (
    "from aegisx_agent.plugins import PluginManifest, define_plugin\n"
    "PLUGIN = define_plugin(PluginManifest("
    "plugin_id='demo', version='1.2.3', tool_name='ping', "
    "description='Return a ping response.'))(lambda: 'pong')\n"
)


def test_plugin_command_group_is_registered(runner: CliRunner) -> None:
    result = _invoke(runner, "--help")

    assert result.exit_code == 0
    assert "plugin" in result.output


def test_plugin_list_shows_empty_state(runner: CliRunner) -> None:
    result = _invoke(runner, "plugin", "list")

    assert result.exit_code == 0, result.output
    assert "No plugins loaded" in result.output


def test_plugin_load_path_then_list_shows_gate_verdicts(runner, tmp_path) -> None:
    plugin_file = tmp_path / "demo_plugin.py"
    plugin_file.write_text(PLUGIN_SOURCE)

    loaded = _invoke(runner, "plugin", "load", str(plugin_file))
    assert loaded.exit_code == 0, loaded.output
    assert "plugin_demo_ping" in loaded.output

    listed = _invoke(runner, "plugin", "list")
    assert listed.exit_code == 0, listed.output
    assert "demo" in listed.output
    assert "1.2.3" in listed.output
    assert "plugin_demo_ping" in listed.output
    # Permission-aware: the verdict column must reflect the gate, not just the risk.
    assert "allowed" in listed.output

    sys.modules.pop("aegisx_external_plugin_demo_plugin", None)


def test_plugin_load_module_and_unload_round_trip(runner, tmp_path) -> None:
    module = ModuleType("aegisx_test_demo_plugins")
    module.PLUGINS = [
        __import__("aegisx_agent.plugins", fromlist=["define_plugin"]).define_plugin(
            __import__("aegisx_agent.plugins", fromlist=["PluginManifest"]).PluginManifest(
                plugin_id="demo",
                version="1.0.0",
                tool_name="ping",
                description="Return a ping response.",
            )
        )(
            lambda: "pong"
        )
    ]
    sys.modules["aegisx_test_demo_plugins"] = module

    try:
        loaded = _invoke(runner, "plugin", "load", "aegisx_test_demo_plugins")
        assert loaded.exit_code == 0, loaded.output
        assert "plugin_demo_ping" in loaded.output

        unloaded = _invoke(runner, "plugin", "unload", "demo")
        assert unloaded.exit_code == 0, unloaded.output
        assert "Unloaded plugin 'demo'" in unloaded.output

        again = _invoke(runner, "plugin", "unload", "demo")
        assert again.exit_code != 0
        assert "No loaded plugin: demo" in again.output
    finally:
        sys.modules.pop("aegisx_test_demo_plugins", None)


def test_plugin_load_rejects_a_broken_file_and_reports_the_error(runner, tmp_path) -> None:
    bad = tmp_path / "bad_plugin.py"
    bad.write_text("raise RuntimeError('boom')\n")

    result = _invoke(runner, "plugin", "load", str(bad))

    assert result.exit_code == 1
    assert "Plugin load failed" in result.output
    sys.modules.pop("aegisx_external_plugin_bad_plugin", None)


def test_slash_plugin_handler_reports_errors_gracefully(runner, tmp_path) -> None:
    from aegisx_agent.cli.commands.plugins import _handle_plugin_command
    from aegisx_agent.config import AgentConfig, LLMProvider

    agent = AegisXAgent(
        AgentConfig(
            llm_provider=LLMProvider.OLLAMA,
            data_dir=str(tmp_path),
            rag_enabled=False,
            web_search_enabled=False,
        )
    )

    _handle_plugin_command("list", agent)  # empty state must not raise
    _handle_plugin_command("", agent)  # usage must not raise
    _handle_plugin_command("unload", agent)  # usage must not raise
    _handle_plugin_command("unload nope", agent)  # unknown id must not raise
    _handle_plugin_command("load", agent)  # missing source must not raise

    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("nope")

    _handle_plugin_command("load nope_module", Broken())  # loader failure is contained
