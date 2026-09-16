"""The ``aegisx update`` command: refresh an installer-based install."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from aegisx_agent.cli import main as cli


@pytest.fixture()
def runner(tmp_path, monkeypatch):
    """Isolated CLI runner: temp data dir, no saved config, no cached agent."""
    monkeypatch.setenv("AEGISX_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("AEGISX_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(cli, "_agent", None)
    return CliRunner()


class FakeRunner:
    """Stand-in for subprocess.run that answers git/pip/uv invocations."""

    def __init__(self, *, current: str, target: str, dirty: bool = False, has_pip: bool = True):
        self.current = current
        self.target = target
        self.dirty = dirty
        self.has_pip = has_pip
        self.calls: list[list[str]] = []
        self.reset_called = False
        self.reinstall_cmds: list[list[str]] = []

    def _git(self, cmd: list[str]) -> SimpleNamespace:
        args = cmd[3:]  # skip ["git", "-C", root]
        if args and args[0] == "fetch":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args and args[0] == "rev-parse":
            sha = self.current if "HEAD" in args[1:] else self.target
            return SimpleNamespace(returncode=0, stdout=sha + "\n", stderr="")
        if args and args[0] == "status":
            out = " M aegisx_agent/x.py\n" if self.dirty else ""
            return SimpleNamespace(returncode=0, stdout=out, stderr="")
        if args and args[0] == "reset":
            self.reset_called = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr=f"unexpected git call: {cmd}")

    def __call__(self, cmd: list[str], **kwargs: Any) -> SimpleNamespace:
        self.calls.append(list(cmd))
        if cmd[0] == "git":
            return self._git(cmd)
        if cmd[1:3] == ["-m", "pip"]:
            if "--version" in cmd:
                code = 0 if self.has_pip else 1
                out = "pip 24.0\n" if code == 0 else ""
                return SimpleNamespace(returncode=code, stdout=out, stderr="")
            if "install" in cmd:
                self.reinstall_cmds.append(list(cmd))
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr=f"unexpected pip call: {cmd}")
        if cmd[0] == "uv" and cmd[1:3] == ["pip", "install"]:
            self.reinstall_cmds.append(list(cmd))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr=f"unexpected call: {cmd}")


def _patch_install_root(monkeypatch, tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.setattr(cli, "_install_root", lambda: root)
    return root


def test_update_without_git_metadata_fails_with_installer_hint(runner, tmp_path, monkeypatch):
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.setattr(cli, "_install_root", lambda: bare)
    result = runner.invoke(cli.app, ["update"])
    assert result.exit_code != 0
    assert "installer" in result.output


def test_update_is_a_no_op_when_already_current(runner, tmp_path, monkeypatch):
    _patch_install_root(monkeypatch, tmp_path)
    fake = FakeRunner(current="abc1234", target="abc1234")
    monkeypatch.setattr(cli.subprocess, "run", fake)
    result = runner.invoke(cli.app, ["update"])
    assert result.exit_code == 0, result.output
    assert "Already up to date" in result.output
    assert not fake.reset_called
    assert fake.reinstall_cmds == []


def test_update_refuses_to_discard_local_changes(runner, tmp_path, monkeypatch):
    _patch_install_root(monkeypatch, tmp_path)
    fake = FakeRunner(current="abc1234", target="def5678", dirty=True)
    monkeypatch.setattr(cli.subprocess, "run", fake)
    result = runner.invoke(cli.app, ["update"])
    assert result.exit_code != 0
    assert "local changes" in result.output
    assert not fake.reset_called


def test_update_resets_and_reinstalls_with_pip(runner, tmp_path, monkeypatch):
    root = _patch_install_root(monkeypatch, tmp_path)
    fake = FakeRunner(current="abc1234", target="def5678")
    monkeypatch.setattr(cli.subprocess, "run", fake)
    result = runner.invoke(cli.app, ["update"])
    assert result.exit_code == 0, result.output
    assert fake.reset_called
    assert "Updated to def5678" in result.output
    assert fake.reinstall_cmds, "pip reinstall should run"
    assert str(root) in " ".join(fake.reinstall_cmds[-1])


def test_update_falls_back_to_uv_when_pip_is_missing(runner, tmp_path, monkeypatch):
    _patch_install_root(monkeypatch, tmp_path)
    fake = FakeRunner(current="abc1234", target="def5678", has_pip=False)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(cli.subprocess, "run", fake)
    result = runner.invoke(cli.app, ["update"])
    assert result.exit_code == 0, result.output
    assert fake.reinstall_cmds, "uv reinstall should run"
    assert fake.reinstall_cmds[-1][:2] == ["uv", "pip"]


def test_update_warns_when_neither_pip_nor_uv_exists(runner, tmp_path, monkeypatch):
    _patch_install_root(monkeypatch, tmp_path)
    fake = FakeRunner(current="abc1234", target="def5678", has_pip=False)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli.subprocess, "run", fake)
    result = runner.invoke(cli.app, ["update"])
    assert result.exit_code == 0
    assert "neither pip nor uv" in result.output
    assert fake.reinstall_cmds == []
