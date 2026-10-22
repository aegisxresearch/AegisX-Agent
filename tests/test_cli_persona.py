"""The ``--persona`` flag: offered where it works, refused when it is wrong.

``aegisx --persona pentester`` is the documented way in, but the flag only
existed on ``chat`` and ``run`` — the bare command answered "No such option"
and the README was describing something that did not exist. These tests pin
the flag to every entry point that starts a session, and pin the refusal of a
name that does not exist, because ``PersonaLoader.get`` answers an unknown
name with the default doctrine and says nothing about the typo.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from aegisx_agent.cli import main as cli
from aegisx_agent.config import AgentConfig, LLMProvider


@pytest.fixture()
def runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    """Isolated CLI runner: temp data dir, no saved config, no cached agent."""
    monkeypatch.setenv("AEGISX_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("AEGISX_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(cli, "_agent", None)
    return CliRunner()


def _config(tmp_path: Path) -> AgentConfig:
    return AgentConfig(
        llm_provider=LLMProvider.OLLAMA,
        data_dir=str(tmp_path),
        rag_enabled=False,
    )


def test_the_bare_command_accepts_persona(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: dict[str, Any] = {}

    def fake_agent(config: AgentConfig | None = None) -> Any:
        received["config"] = config
        return SimpleNamespace(config=config)

    monkeypatch.setattr(cli, "_get_agent", fake_agent)
    monkeypatch.setattr(cli, "_run_chat", lambda agent, no_stream=False: None)

    result = runner.invoke(cli.app, ["--persona", "pentester"])

    assert result.exit_code == 0, result.output
    assert received["config"].persona == "pentester"


@pytest.mark.parametrize(
    "args",
    [
        ["--help"],
        ["chat", "--help"],
        ["plan", "--help"],
        ["run", "--help"],
    ],
)
def test_every_session_entry_point_advertises_persona(
    runner: CliRunner, args: list[str]
) -> None:
    result = runner.invoke(cli.app, args)

    assert result.exit_code == 0, result.output
    assert "--persona" in result.output


def test_a_mistyped_persona_is_refused_before_a_session_starts(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[str] = []
    monkeypatch.setattr(cli, "_run_chat", lambda *a, **k: started.append("ran"))

    result = runner.invoke(cli.app, ["--persona", "pentest"])

    assert result.exit_code != 0
    assert "unknown persona 'pentest'" in result.output
    # The list of what does exist is the useful part of the message.
    assert "pentester" in result.output
    assert started == []


def test_apply_persona_sets_a_known_name(tmp_path: Path) -> None:
    config = _config(tmp_path)

    cli._apply_persona(config, "coder")

    assert config.persona == "coder"


def test_apply_persona_leaves_the_config_alone_without_a_flag(tmp_path: Path) -> None:
    config = _config(tmp_path)

    cli._apply_persona(config, None)

    assert config.persona == "default"


def test_apply_persona_rejects_an_unknown_name(tmp_path: Path) -> None:
    config = _config(tmp_path)

    with pytest.raises(typer.BadParameter, match="unknown persona"):
        cli._apply_persona(config, "pentest")
