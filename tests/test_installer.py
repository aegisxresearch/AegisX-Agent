"""installer.sh — the shell installer that puts ``aegisx`` on PATH.

The installer is the first thing a new user runs, and it runs on machines we
do not control: no ``uv``, no ``python3-venv``, BSD ``sed`` instead of GNU,
and a shell rc file full of leftovers from an earlier manual install. These
tests pin the parts that broke in the wild:

* ``sed -i`` is spelled differently on GNU and BSD — every in-place edit goes
  through one helper instead of calling ``sed -i`` directly.
* ``rm -rf`` on a user-supplied path needs a guard: an empty string, ``/`` and
  ``$HOME`` must never reach it.
* a venv without pip cannot install anything.
* ``--dir`` used to be silently ignored when the installer ran from a checkout.
* the wrapper must actually run, not merely exist.

Everything here is offline: no network, no venv creation, no real install.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aegisx_agent.cli.app import app

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "installer.sh"

#: Absolute path to bash: the sandbox test replaces PATH, so the interpreter
#: must not be looked up again.
BASH = shutil.which("bash") or "/bin/bash"

#: Commands the installer needs before it reaches the Python step. The sandbox
#: PATH is limited to these so the install stops at "python3 not found" —
#: proving the ``--dir`` copy happened without installing anything.
SANDBOX_COMMANDS = ("dirname", "mkdir", "tar")


def _bash(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _extract_functions(source: str, names: list[str]) -> str:
    """Pull single functions out of the installer so they can be tested alone."""
    lines = source.splitlines()
    chunks: list[str] = []
    for name in names:
        pattern = re.compile(rf"^{re.escape(name)}\(\)\s*\{{")
        start = next(
            (index for index, line in enumerate(lines) if pattern.match(line)),
            None,
        )
        assert start is not None, f"{name}() not found in installer.sh"
        end = start
        while end < len(lines) and lines[end] != "}":
            end += 1
        chunks.append("\n".join(lines[start : end + 1]))
    return "\n\n".join(chunks)


def _fake_home(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    """Build an isolated HOME/BIN_DIR/DATA_DIR triple for the installer."""
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    data_dir = home / ".aegisx"
    env = {
        **os.environ,
        "HOME": str(home),
        "XDG_BIN_HOME": str(bin_dir),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "AEGISX_DATA_DIR": str(data_dir),
        "NO_COLOR": "1",
    }
    return env, bin_dir, data_dir


def _write_wrapper(bin_dir: Path, install_dir: Path) -> Path:
    wrapper = bin_dir / "aegisx"
    wrapper.write_text(
        f'#!/usr/bin/env bash\nexec "{install_dir}/aegisx" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper


# ─────────────────────────────────────────────────────────────────────────────
# The installer script itself
# ─────────────────────────────────────────────────────────────────────────────


def test_installer_is_valid_bash() -> None:
    result = _bash(f"bash -n {INSTALLER}")
    assert result.returncode == 0, result.stderr


def test_installer_help_documents_the_options() -> None:
    result = _bash(f"bash {INSTALLER} --help")
    assert result.returncode == 0, result.stderr
    assert "--dir" in result.stdout
    assert "--uninstall" in result.stdout


def test_installer_is_executable_in_the_git_index() -> None:
    """A non-executable installer.sh breaks the documented `curl | bash` flow."""
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    result = _bash(f"git -C {REPO_ROOT} ls-files -s -- installer.sh")
    if result.returncode != 0:
        pytest.skip("installer.sh is not tracked yet")
    mode = result.stdout.split()[0]
    assert mode == "100755", f"installer.sh is staged as {mode}, expected 100755"


# ─────────────────────────────────────────────────────────────────────────────
# Shell helpers
# ─────────────────────────────────────────────────────────────────────────────


def test_safe_rm_rf_refuses_the_home_directory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    script = _extract_functions(
        INSTALLER.read_text(encoding="utf-8"), ["err", "die", "safe_rm_rf"]
    )
    script += '\nsafe_rm_rf "$HOME"\n'

    result = _bash(script, {**os.environ, "HOME": str(home)})

    assert result.returncode != 0
    assert "refusing to remove" in result.stderr
    assert home.exists()


def test_safe_rm_rf_refuses_the_filesystem_root(tmp_path: Path) -> None:
    script = _extract_functions(
        INSTALLER.read_text(encoding="utf-8"), ["err", "die", "safe_rm_rf"]
    )
    script += '\nsafe_rm_rf "/"\n'

    result = _bash(script)

    assert result.returncode != 0
    assert "refusing to remove" in result.stderr


def test_safe_rm_rf_removes_an_ordinary_directory(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    (victim / "nested").mkdir(parents=True)
    (victim / "nested" / "file.txt").write_text("x", encoding="utf-8")
    script = _extract_functions(
        INSTALLER.read_text(encoding="utf-8"), ["err", "die", "safe_rm_rf"]
    )
    script += f'\nsafe_rm_rf "{victim}"\n'

    result = _bash(script)

    assert result.returncode == 0, result.stderr
    assert not victim.exists()


def test_sed_delete_line_removes_the_requested_line(tmp_path: Path) -> None:
    """The helper must work with both GNU sed and BSD sed spellings."""
    target = tmp_path / "rc"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    script = _extract_functions(INSTALLER.read_text(encoding="utf-8"), ["sed_delete_line"])
    script += f'\nsed_delete_line "{target}" 2\n'

    result = _bash(script)

    assert result.returncode == 0, result.stderr
    assert target.read_text(encoding="utf-8") == "one\nthree\n"


def test_copy_checkout_copies_the_tree_without_local_junk(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "pkg").mkdir(parents=True)
    (source / "pkg" / "__pycache__").mkdir()
    (source / ".venv").mkdir()
    (source / ".git").mkdir()
    (source / "pkg" / "module.py").write_text("x = 1\n", encoding="utf-8")
    (source / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    destination = tmp_path / "destination"

    script = _extract_functions(
        INSTALLER.read_text(encoding="utf-8"), ["err", "die", "info", "copy_checkout"]
    )
    script += f'\ncopy_checkout "{source}" "{destination}"\n'

    result = _bash(script)

    assert result.returncode == 0, result.stderr
    assert (destination / "pyproject.toml").exists()
    assert (destination / "pkg" / "module.py").exists()
    assert not (destination / ".venv").exists()
    assert not (destination / ".git").exists()
    assert not (destination / "pkg" / "__pycache__").exists()


def test_copy_checkout_refuses_a_destination_inside_the_checkout(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    script = _extract_functions(
        INSTALLER.read_text(encoding="utf-8"), ["err", "die", "info", "copy_checkout"]
    )
    script += f'\ncopy_checkout "{source}" "{source / "inside"}"\n'

    result = _bash(script)

    assert result.returncode != 0
    assert "inside the checkout" in result.stderr


# ─────────────────────────────────────────────────────────────────────────────
# Uninstall
# ─────────────────────────────────────────────────────────────────────────────


def test_uninstall_removes_the_install_recorded_in_the_wrapper(tmp_path: Path) -> None:
    env, bin_dir, _data_dir = _fake_home(tmp_path)
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    _write_wrapper(bin_dir, install_dir)

    result = _bash(f"bash {INSTALLER} --uninstall --yes", env)

    assert result.returncode == 0, result.stderr
    assert not install_dir.exists()
    assert not (bin_dir / "aegisx").exists()


def test_uninstall_keeps_a_checkout_source_tree(tmp_path: Path) -> None:
    """Uninstalling must never throw away the user's working copy."""
    env, bin_dir, _data_dir = _fake_home(tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (checkout / "keep-me.txt").write_text("source", encoding="utf-8")
    (checkout / ".venv").mkdir()
    installer_copy = checkout / "installer.sh"
    installer_copy.write_text(INSTALLER.read_text(encoding="utf-8"), encoding="utf-8")
    _write_wrapper(bin_dir, checkout)

    result = _bash(f"bash {installer_copy} --uninstall --yes", env)

    assert result.returncode == 0, result.stderr
    assert (checkout / "keep-me.txt").exists()
    assert not (checkout / ".venv").exists()
    assert not (bin_dir / "aegisx").exists()


def test_uninstall_refuses_to_delete_the_filesystem_root(tmp_path: Path) -> None:
    env, bin_dir, _data_dir = _fake_home(tmp_path)
    _write_wrapper(bin_dir, Path("/"))

    result = _bash(f"bash {INSTALLER} --uninstall --yes", env)

    assert result.returncode != 0
    assert "refusing to remove" in result.stderr


def test_uninstall_purges_the_data_directory_when_asked(tmp_path: Path) -> None:
    env, bin_dir, data_dir = _fake_home(tmp_path)
    data_dir.mkdir(parents=True)
    (data_dir / "config.json").write_text("{}", encoding="utf-8")
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    _write_wrapper(bin_dir, install_dir)

    result = _bash(f"bash {INSTALLER} --uninstall --purge --yes", env)

    assert result.returncode == 0, result.stderr
    assert not data_dir.exists()


def test_uninstall_cleans_dead_aliases_from_the_shell_rc(tmp_path: Path) -> None:
    env, bin_dir, _data_dir = _fake_home(tmp_path)
    rc = Path(env["HOME"]) / ".bashrc"
    live_binary = tmp_path / "live-aegisx"
    live_binary.write_text("#!/bin/sh\n", encoding="utf-8")
    rc.write_text(
        "\n".join(
            [
                "# keep this comment about aegisx",
                "alias aegisx=/nonexistent/aegisx-old",
                f"alias aegisx-live={live_binary}",
                "export PATH=/nonexistent/aegisx/bin:$PATH",
                "export AEGISX_PATH=/nonexistent/aegisx/venv",
                "",
            ]
        ),
        encoding="utf-8",
    )
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    _write_wrapper(bin_dir, install_dir)

    result = _bash(f"bash {INSTALLER} --uninstall --yes", env)

    assert result.returncode == 0, result.stderr
    cleaned = rc.read_text(encoding="utf-8")
    assert "# keep this comment about aegisx" in cleaned
    assert "alias aegisx=/nonexistent/aegisx-old" not in cleaned
    assert "export PATH=/nonexistent/aegisx/bin:$PATH" not in cleaned
    # A live alias and an unrelated AEGISX_* variable are not dead entries.
    assert "alias aegisx-live=" in cleaned
    assert "export AEGISX_PATH=" in cleaned


# ─────────────────────────────────────────────────────────────────────────────
# --dir is honoured when running from a checkout
# ─────────────────────────────────────────────────────────────────────────────


def test_dir_option_copies_the_checkout_instead_of_ignoring_it(tmp_path: Path) -> None:
    """--dir used to be dropped on the floor when run from a checkout."""
    env, _bin_dir, _data_dir = _fake_home(tmp_path)
    sandbox = tmp_path / "sandbox-bin"
    sandbox.mkdir()
    for command in SANDBOX_COMMANDS:
        located = shutil.which(command)
        assert located is not None, f"{command} is not available"
        (sandbox / command).symlink_to(located)
    # A PATH without python3/uv: the installer copies the tree and then stops
    # at the Python step, which is enough to prove the copy happened.
    env["PATH"] = str(sandbox)
    destination = tmp_path / "explicit-install"

    result = _bash(f"{BASH} {INSTALLER} --dir {destination} --yes", env)

    assert result.returncode != 0
    assert "python3 not found" in result.stderr
    assert (destination / "pyproject.toml").exists()
    assert (destination / "aegisx").exists()
    assert not (destination / ".venv").exists()


# ─────────────────────────────────────────────────────────────────────────────
# The CLI reports its version
# ─────────────────────────────────────────────────────────────────────────────


def test_cli_version_flag_prints_the_version() -> None:
    from aegisx_agent import __version__

    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0, result.output
    assert f"AegisX Agent {__version__}" in result.output


def test_cli_version_flag_works_from_the_module_entrypoint() -> None:
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "aegisx_agent.cli", "--version"],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        cwd=str(REPO_ROOT),
    )

    assert result.returncode == 0, result.stderr
    assert "AegisX Agent" in result.stdout


def test_the_closing_message_points_at_the_security_commands() -> None:
    """The last thing an install prints is the first thing a user will try.

    It listed only ``aegisx``, ``aegisx run``, and ``config-info``, so a fresh
    install gave no hint that the assessment engine or the pentester persona
    existed — the script was current while its message was a release behind.
    """
    source = INSTALLER.read_text(encoding="utf-8")

    for command in (
        "aegisx cyber capabilities",
        "aegisx cyber scan",
        "aegisx cyber sast",
        "aegisx cyber audit",
        "aegisx --persona pentester",
    ):
        assert command in source
    assert "Only assess systems you own" in source
