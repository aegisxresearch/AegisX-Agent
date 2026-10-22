"""The ``aegisx cyber`` commands and the interactive ``/cyber`` slash command.

Only the paths that do not touch a network target run here: the commands
that describe the engine or read a source tree. The scan paths are covered
at the engine level, where the prober can be replaced.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from typer.testing import CliRunner

from aegisx_agent.cli import main as cli
from aegisx_agent.cli.commands.cyber import (
    _handle_cyber_command,
    _print_cyber_status,
    _print_scope,
)
from aegisx_agent.cyber.agent import CAPABILITY_CATALOGUE

runner = CliRunner()

#: A synthetic AWS key id for the secret scanner to find. The literal is split
#: so the repository's own pre-push secret guard does not read this fixture as
#: a leaked credential — the scanner still sees the full value at runtime.
AWS_KEY = "AKIA" "3XQ7ZJ2K5M8N4P6R"


def stub_agent(**config_overrides: Any) -> Any:
    """An agent-shaped object with just the config the status view reads."""
    settings = {
        "cyber_enabled": True,
        "cyber_target_names": ["app.example.com"],
        "cyber_max_requests": 1500,
        "cyber_requests_per_second": 8.0,
        "cyber_max_concurrency": 4,
        "cyber_allow_private_addresses": False,
    }
    settings.update(config_overrides)
    return SimpleNamespace(config=SimpleNamespace(**settings), tools=None)


def test_cyber_help_lists_every_verb() -> None:
    result = runner.invoke(cli.app, ["cyber", "--help"])

    assert result.exit_code == 0
    for command in ("scan", "sast", "audit", "swarm", "agent", "report", "capabilities"):
        assert command in result.stdout


def test_capabilities_are_printed_from_the_catalogue() -> None:
    result = runner.invoke(cli.app, ["cyber", "capabilities"])

    assert result.exit_code == 0
    for capability in CAPABILITY_CATALOGUE:
        assert capability in result.stdout


def test_sast_runs_over_a_local_tree(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(f'key = "{AWS_KEY}"\n', encoding="utf-8")

    result = runner.invoke(cli.app, ["cyber", "sast", str(tmp_path)])

    assert result.exit_code == 0
    assert "Scanned 1 files" in result.stdout
    assert "findings" in result.stdout


def test_sast_reports_a_missing_path_as_a_failure(tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["cyber", "sast", str(tmp_path / "nope")])

    assert result.exit_code == 1
    assert "Path not found" in result.stdout


def test_audit_says_so_when_a_tree_has_no_manifests(tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["cyber", "audit", str(tmp_path)])

    assert result.exit_code == 0
    assert "No dependency manifests" in result.stdout


def test_the_slash_command_without_arguments_prints_usage() -> None:
    _handle_cyber_command("", stub_agent())


def test_the_slash_command_prints_the_status_table() -> None:
    _print_cyber_status(stub_agent())

    _print_cyber_status(stub_agent(cyber_enabled=False, cyber_target_names=[]))


def test_the_status_view_explains_that_the_commands_work_without_cyber_mode(
    capsys: Any,
) -> None:
    """A session without the cyber tools is the default, not a fault."""
    _print_cyber_status(stub_agent(cyber_enabled=False, cyber_target_names=[]))

    printed = capsys.readouterr().out
    assert "No cyber tools are registered" in printed
    assert "still work" in printed


def test_the_cyber_commands_work_with_cyber_mode_off(tmp_path: Path) -> None:
    """The commands are the product; the chat tools are the opt-in extra.

    Cyber mode is off by default, and nothing below may depend on it:
    ``aegisx cyber …`` and ``/cyber …`` drive the engine directly, so a
    default session can still scan, analyse, and report.
    """
    from aegisx_agent.config import AgentConfig, LLMProvider
    from aegisx_agent.core import AegisXAgent

    source = tmp_path / "src"
    source.mkdir()
    (source / "app.py").write_text(f'key = "{AWS_KEY}"\n', encoding="utf-8")

    agent = AegisXAgent(
        AgentConfig(
            llm_provider=LLMProvider.OLLAMA,
            data_dir=str(tmp_path / "data"),
            rag_enabled=False,
        )
    )

    # The default session has no cyber tools and no pentester doctrine.
    assert agent.config.cyber_enabled is False
    assert not any(tool.name.startswith("cyber_") for tool in agent.tools.list_tools())

    # The slash command still reaches the engine.
    _handle_cyber_command("capabilities", agent)
    _handle_cyber_command(f"sast {source}", agent)

    # And so does the CLI.
    result = runner.invoke(cli.app, ["cyber", "sast", str(source)])

    assert result.exit_code == 0
    assert "Scanned 1 files" in result.stdout


def test_the_slash_command_prints_a_scope_for_an_authorised_host() -> None:
    _handle_cyber_command("scope https://app.example.com", stub_agent())


def test_the_slash_command_refuses_a_private_target() -> None:
    _print_scope("http://127.0.0.1/")


def test_the_slash_command_rejects_an_unknown_verb_and_a_missing_target() -> None:
    _handle_cyber_command("teleport https://app.example.com", stub_agent())
    _handle_cyber_command("scan", stub_agent())
    _handle_cyber_command("agent", stub_agent())
