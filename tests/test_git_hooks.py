"""The pre-push secret guard in ``.githooks/``.

The guard is the last thing between a leaked credential and a public remote.
It is a shell script, so its failure mode is not an exception — it is a
pattern that quietly matches nothing. A guard that reports success while
letting a private key through is worse than no guard, because it is trusted.
These tests drive the real hook inside a throwaway git repository and assert
it blocks exactly what it claims to block.

* Every declared pattern must actually match. A pattern that starts with
  ``-`` (``-----BEGIN ... PRIVATE KEY-----``) is parsed by ``grep`` as an
  option unless it is separated with ``--``; without that separator the PEM
  check is dead code and the guard passes while printing an error nobody
  reads.
* A clean tree must not be blocked. A guard that always fails is no guard.
* The guard must work on the first push from a fresh clone, where there is
  no ``origin/main`` to diff against and the committed tree is the only
  thing worth scanning.

The credential-shaped literals below are assembled at runtime from
fragments, so this file is not itself read as a leak by the guard it
tests.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / ".githooks" / "pre-push"

#: Absolute path to bash: the hook is a bash script and must not be looked
#: up through PATH, which the sandboxed runs below replace.
BASH = shutil.which("bash") or "/bin/bash"

#: Leaked secrets, assembled at runtime. Each is a plausible shape for the
#: guard to catch, and none of them appears verbatim in this file. The
#: lengths match the guard's patterns exactly: a GitHub token carries 36
#: characters after the prefix, an AWS key id 16, a manicode token 32+.
GITHUB_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9" * 2
AWS_KEY_ID = "AKIA" + "3XQ7ZJ2K5M8N4P6R"
PRIVATE_KEY = "-----BEGIN " + "RSA PRIVATE KEY-----\nZmFrZS1rZXktbWF0ZXJpYWw=\n"
MANICODE_TOKEN = "manicode-" + "a1B2c3D4e5F6g7H8" * 2

#: A file body that contains no credential-shaped text.
CLEAN_BODY = "print('hello world')\n"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git inside ``repo`` without touching the user's global config."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _init_repo(repo: Path) -> None:
    """Create a repository with one commit and a matching ``origin/main``."""
    repo.mkdir(parents=True, exist_ok=True)
    assert _git(repo, "init", "-b", "main").returncode == 0
    _git(repo, "config", "user.email", "guard@example.invalid")
    _git(repo, "config", "user.name", "Guard Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    # A remote-tracking ref stands in for the upstream branch a push would
    # compare against. The hook diffs ``origin/main..HEAD``.
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert _git(repo, "update-ref", "refs/remotes/origin/main", head).returncode == 0


def _commit_body(repo: Path, body: str, name: str = "leaked.txt") -> None:
    """Commit ``body`` as a new file on top of the current branch."""
    (repo / name).write_text(body, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", f"add {name}")


def _run_hook(repo: Path) -> subprocess.CompletedProcess[str]:
    """Run the pre-push guard against ``repo``."""
    return subprocess.run(
        [BASH, str(HOOK)],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _assert_blocked(repo: Path, description: str) -> subprocess.CompletedProcess[str]:
    result = _run_hook(repo)
    assert result.returncode != 0, (
        f"the guard let a {description} through; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "LEAK DETECTED" in result.stdout, (
        f"the guard blocked a {description} without saying why; stdout={result.stdout!r}"
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Every declared pattern must actually match
# ─────────────────────────────────────────────────────────────────────────────


def test_guard_blocks_a_github_token(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_body(repo, f"TOKEN = {GITHUB_TOKEN!r}\n")

    _assert_blocked(repo, "GitHub token")


def test_guard_blocks_an_aws_key_id(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_body(repo, f"AWS_KEY = {AWS_KEY_ID!r}\n")

    _assert_blocked(repo, "AWS access key id")


def test_guard_blocks_a_pem_private_key(tmp_path: Path) -> None:
    """The regression: the PEM pattern starts with ``-``.

    ``grep`` reads a pattern that begins with a dash as an option list, so
    the check died with an "unrecognized option" error on stderr and matched
    nothing. The guard still exited 0, so a private key pushed happily.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_body(repo, PRIVATE_KEY, name="id_rsa")

    result = _assert_blocked(repo, "PEM private key")
    assert "unrecognized option" not in result.stderr, (
        "the PEM pattern was passed to grep without a '--' separator, so the "
        f"check is dead code; stderr={result.stderr!r}"
    )


def test_guard_blocks_a_manicode_token(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_body(repo, f"AUTH = {MANICODE_TOKEN!r}\n")

    _assert_blocked(repo, "manicode token")


# ─────────────────────────────────────────────────────────────────────────────
# A clean tree must not be blocked
# ─────────────────────────────────────────────────────────────────────────────


def test_guard_allows_a_clean_tree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_body(repo, CLEAN_BODY, name="app.py")

    result = _run_hook(repo)

    assert result.returncode == 0, (
        f"the guard blocked a clean push; stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "LEAK DETECTED" not in result.stdout


def test_guard_allows_a_clean_first_push_from_a_fresh_clone(tmp_path: Path) -> None:
    """No ``origin/main`` to diff against — the committed tree is the target."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    assert _git(repo, "init", "-b", "main").returncode == 0
    _git(repo, "config", "user.email", "guard@example.invalid")
    _git(repo, "config", "user.name", "Guard Test")
    _git(repo, "config", "commit.gpgsign", "false")
    _commit_body(repo, CLEAN_BODY, name="app.py")

    result = _run_hook(repo)

    assert result.returncode == 0, (
        f"the guard blocked a clean first push; stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )


def test_guard_blocks_a_leak_on_a_first_push_from_a_fresh_clone(tmp_path: Path) -> None:
    """The same fresh clone, but the tree carries a credential."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    assert _git(repo, "init", "-b", "main").returncode == 0
    _git(repo, "config", "user.email", "guard@example.invalid")
    _git(repo, "config", "user.name", "Guard Test")
    _git(repo, "config", "commit.gpgsign", "false")
    _commit_body(repo, PRIVATE_KEY, name="id_rsa")

    _assert_blocked(repo, "PEM private key")


# ─────────────────────────────────────────────────────────────────────────────
# The guard itself
# ─────────────────────────────────────────────────────────────────────────────


def test_guard_is_valid_bash() -> None:
    result = subprocess.run(
        [BASH, "-n", str(HOOK)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_guard_is_executable_in_the_git_index() -> None:
    """A non-executable hook is silently ignored by git."""
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-s", "--", ".githooks/pre-push"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        pytest.skip(".githooks/pre-push is not tracked yet")
    mode = result.stdout.split()[0]
    assert mode == "100755", f".githooks/pre-push is staged as {mode}, expected 100755"
