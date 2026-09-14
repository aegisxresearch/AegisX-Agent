"""cli/main.py plumbing: config load/save/merge, autodetect, wizard, prompter.

These are the parts every command depends on; the tests keep the filesystem
and stdin fake so they are deterministic.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from aegisx_agent.cli import main as cli
from aegisx_agent.config import LLMProvider


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """Point CONFIG_FILE at a temp path and clear the cached agent."""
    monkeypatch.setattr(cli, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(cli, "_agent", None)
    return tmp_path


# === _load_saved_config / _save_config ===


def test_load_saved_config_reads_valid_json_and_survives_garbage(isolated) -> None:
    assert cli._load_saved_config() is None  # file missing

    cli.CONFIG_FILE.write_text(json.dumps({"provider": "ollama"}), encoding="utf-8")
    assert cli._load_saved_config() == {"provider": "ollama"}

    cli.CONFIG_FILE.write_text("{broken json", encoding="utf-8")
    assert cli._load_saved_config() is None


def test_save_config_writes_provider_model_key_and_url(isolated, monkeypatch) -> None:
    from aegisx_agent.core.config import AgentConfig

    config = AgentConfig(
        llm_provider=LLMProvider.CUSTOM,
        custom_base_url="http://localhost:9999/v1",
        custom_api_key="secret",
        custom_model="m1",
    )
    cli._save_config(config)

    data = json.loads(cli.CONFIG_FILE.read_text())
    assert data == {
        "provider": "custom",
        "model": "m1",
        "api_key": "secret",
        "base_url": "http://localhost:9999/v1",
    }


# === _get_config ===


def test_get_config_applies_saved_provider_and_model(isolated, monkeypatch) -> None:
    cli.CONFIG_FILE.write_text(
        json.dumps({"provider": "ollama", "model": "custom-llama", "base_url": "http://x:1"}),
        encoding="utf-8",
    )
    monkeypatch.delenv("AEGISX_LLM_PROVIDER", raising=False)

    config = cli._get_config()

    assert config.llm_provider is LLMProvider.OLLAMA
    assert config.ollama_model == "custom-llama"
    assert config.ollama_base_url == "http://x:1"


def test_get_config_falls_back_to_custom_on_an_unknown_saved_provider(isolated) -> None:
    cli.CONFIG_FILE.write_text(json.dumps({"provider": "hal9000"}), encoding="utf-8")

    config = cli._get_config()

    assert config.llm_provider is LLMProvider.CUSTOM


def test_get_config_cli_arguments_override_saved_values(isolated, monkeypatch) -> None:
    cli.CONFIG_FILE.write_text(json.dumps({"provider": "ollama", "model": "old"}), encoding="utf-8")
    monkeypatch.delenv("AEGISX_LLM_PROVIDER", raising=False)

    config = cli._get_config(provider=LLMProvider.OLLAMA, model="new-model")

    assert config.ollama_model == "new-model"


def test_get_config_custom_url_argument_lands_on_custom_base_url(isolated, monkeypatch) -> None:
    monkeypatch.setenv("AEGISX_LLM_PROVIDER", "custom")

    config = cli._get_config(custom_url="http://endpoint/v1")

    assert config.custom_base_url == "http://endpoint/v1"


def test_get_config_autodetects_a_running_local_model(isolated, monkeypatch) -> None:
    monkeypatch.delenv("AEGISX_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("AEGISX_CUSTOM_BASE_URL", "")  # nothing configured

    calls: list[str] = []

    def fake_detect() -> tuple[str, str] | None:
        calls.append("detect")
        return ("http://localhost:11434", "llama3.2")

    monkeypatch.setattr("aegisx_agent.llm.autodetect.detect_local_provider", fake_detect)

    config = cli._get_config()

    assert calls == ["detect"]
    assert config.llm_provider is LLMProvider.OLLAMA
    assert config.ollama_model == "llama3.2"


# === _apply_permission_mode ===


def test_apply_permission_mode_validates_and_applies() -> None:
    from aegisx_agent.core.config import AgentConfig

    config = AgentConfig(llm_provider=LLMProvider.OLLAMA)
    cli._apply_permission_mode(config, "read-only")
    assert config.permission_mode.value == "read-only"

    cli._apply_permission_mode(config, None)  # no-op

    import typer

    with pytest.raises(typer.BadParameter, match="unknown permission mode"):
        cli._apply_permission_mode(config, "yolo")


# === _permission_prompt ===


def test_permission_prompt_deny_allow_always_and_default(isolated, capsys, monkeypatch) -> None:
    from aegisx_agent.security.permissions import PermissionRequest
    from aegisx_agent.tools.base import ToolRisk

    request = PermissionRequest(
        tool="shell", risk=ToolRisk.DANGEROUS, summary="command=rm -rf", arguments={}
    )

    class FakeGate:
        def __init__(self) -> None:
            self.allowed: list[str] = []

        def allow(self, tool: str) -> None:
            self.allowed.append(tool)

    agent = type("A", (), {"permission_gate": FakeGate()})()
    monkeypatch.setattr(cli, "_agent", agent)

    answers = iter(["a", "n", "y"])
    monkeypatch.setattr(cli, "Prompt", type("P", (), {"ask": staticmethod(lambda *a, **k: next(answers))}))  # noqa: E501

    import asyncio

    assert asyncio.run(cli._permission_prompt(request)) is True  # "a" always-allow
    assert agent.permission_gate.allowed == ["shell"]

    assert asyncio.run(cli._permission_prompt(request)) is False  # "n"
    assert asyncio.run(cli._permission_prompt(request)) is True  # "y"


# === slash-handler re-exports stay importable from main ===


def test_reexports_remain_on_main_for_tests_and_callers() -> None:
    for name in (
        "_handle_code_command",
        "_handle_git_command",
        "_handle_test_command",
        "_handle_schedule_command",
        "_handle_plugin_command",
        "_handle_permissions_command",
        "_split_flags",
        "_flags_to_schedule",
        "_resolve_schedule",
        "_print_schedule_logs",
        "_print_schedule_results",
        "_handle_slash_command",
        "_run_chat",
        "AnimatedProgress",
        "app",
        "console",
    ):
        assert hasattr(cli, name), f"missing re-export: {name}"


# === chat command persona artifact ===


def test_chat_command_accepts_a_persona_flag(isolated, monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run_chat(agent, no_stream=False):
        captured["persona"] = agent.config.persona

    monkeypatch.setattr(cli, "_run_chat", fake_run_chat)
    monkeypatch.setenv("AEGISX_LLM_PROVIDER", "ollama")

    runner = CliRunner()
    result = runner.invoke(cli.app, ["chat", "--persona", "coder"])

    assert result.exit_code == 0, result.output
    assert captured["persona"] == "coder"
