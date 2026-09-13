"""What the agent knows about the folder it was started in.

An agent that does not know where it is cannot work on a codebase: it does not
know the absolute path for file tools, which language the project uses, whether
the working tree is dirty, or that the folder ships instructions in
``AGENTS.md``. This module detects all of that once, cheaply, and turns it into
a short block for the system prompt.

Everything here is best effort and bounded: a missing ``git``, an unreadable
directory, or a huge tree must never stop the agent from starting.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

#: Project markers, most specific first: (file, label).
_STACK_MARKERS: tuple[tuple[str, str], ...] = (
    ("pyproject.toml", "Python"),
    ("setup.py", "Python"),
    ("requirements.txt", "Python"),
    ("package.json", "Node"),
    ("tsconfig.json", "TypeScript"),
    ("go.mod", "Go"),
    ("Cargo.toml", "Rust"),
    ("pom.xml", "Java"),
    ("build.gradle", "Java"),
    ("Gemfile", "Ruby"),
    ("composer.json", "PHP"),
    ("CMakeLists.txt", "C/C++"),
    ("Makefile", "Make"),
    ("Dockerfile", "Docker"),
)

#: Files that carry project instructions for the agent, most authoritative first.
_INSTRUCTION_FILES: tuple[str, ...] = (
    "AGENTS.md",
    "CLAUDE.md",
    "AEGISX.md",
    ".aegisx.md",
    ".cursorrules",
    ".github/copilot-instructions.md",
)

#: Directories that are never worth counting or walking into.
_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".next",
        "target",
        ".idea",
        ".vscode",
        ".cache",
        ".gradle",
        ".terraform",
        ".turbo",
        ".pnpm-store",
        "coverage",
    }
)

_MAX_FILE_COUNT = 20_000
_MAX_INSTRUCTIONS_CHARS = 4_000
_GIT_TIMEOUT = 2.0


@dataclass
class ProjectContext:
    """A snapshot of the working directory the agent was launched in."""

    root: Path
    stacks: list[str] = field(default_factory=list)
    git_branch: str | None = None
    git_changed: int = 0
    git_is_repo: bool = False
    git_has_commits: bool = True
    file_count: int = 0
    instructions_file: str | None = None
    instructions: str = ""

    @property
    def name(self) -> str:
        return self.root.name or str(self.root)

    def git_label(self) -> str:
        """Short description of the repository state."""
        if not self.git_is_repo:
            return "not a git repo"
        if not self.git_has_commits:
            return "git (no commits yet)"
        return f"git {self.git_branch or 'detached HEAD'}"

    def git_dirt_label(self) -> str:
        if not self.git_is_repo:
            return ""
        return f"{self.git_changed} changed" if self.git_changed else "clean"

    def summary(self) -> str:
        """One line for the welcome banner."""
        parts = [str(self.root)]
        if self.stacks:
            parts.append(", ".join(self.stacks))
        if self.git_is_repo:
            parts.append(f"{self.git_label()}, {self.git_dirt_label()}")
        else:
            parts.append("not a git repo")
        parts.append(f"{self.file_count} files")
        return " • ".join(parts)

    def to_prompt(self) -> str:
        """Environment block for the system prompt."""
        lines = [
            "[Workspace]",
            f"Working directory: {self.root}",
            "Relative paths in tool calls are resolved against this directory, "
            "so prefer relative paths.",
        ]
        if self.stacks:
            lines.append(f"Detected stack: {', '.join(self.stacks)}")
        if self.git_is_repo:
            dirt = (
                f"{self.git_changed} uncommitted change(s)"
                if self.git_changed
                else "working tree clean"
            )
            if self.git_has_commits:
                lines.append(f"Git: branch '{self.git_branch or 'detached HEAD'}', {dirt}")
            else:
                lines.append(f"Git: repository with no commits yet, {dirt}")
        lines.append(f"Files in the project (excluding vendor/build dirs): {self.file_count}")

        if self.instructions and self.instructions_file:
            lines.append("")
            lines.append(
                f"Project instructions from {self.instructions_file} — follow these:"
            )
            lines.append(self.instructions)

        return "\n".join(lines)


def _count_files(root: Path, limit: int = _MAX_FILE_COUNT) -> int:
    """Count files, skipping vendor/build directories."""
    count = 0
    for current, dirs, files in os.walk(root, followlinks=False):
        # Only the ignore list is skipped: a hidden directory such as
        # ``.freebuff`` is part of the project and must be counted.
        dirs[:] = [
            d for d in dirs if d not in _IGNORED_DIRS and not d.endswith(".egg-info")
        ]
        count += len(files)
        if count >= limit:
            return limit
        # Depth guard: a stray symlink farm should not turn into a full walk.
        if current.count(os.sep) - str(root).count(os.sep) >= 12:
            dirs[:] = []
    return count


def _git(root: Path, *args: str) -> str | None:
    """Run a read-only git command, or return ``None`` if that is not possible."""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(root), *args],  # noqa: S607 - git from PATH is intended
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _read_instructions(root: Path) -> tuple[str | None, str]:
    """Return (filename, text) of the first project instructions file found."""
    for name in _INSTRUCTION_FILES:
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        text = text.strip()
        if not text:
            continue
        if len(text) > _MAX_INSTRUCTIONS_CHARS:
            text = text[:_MAX_INSTRUCTIONS_CHARS] + "\n... [truncated]"
        return name, text
    return None, ""


def detect_project(start: Path | str | None = None) -> ProjectContext:
    """Detect the project rooted at ``start`` (defaults to the current directory)."""
    try:
        root = Path(start).expanduser().resolve() if start else Path.cwd().resolve()
    except OSError:  # pragma: no cover - resolve() on a deleted cwd
        root = Path(start or ".")

    if not root.is_dir():
        return ProjectContext(root=root, file_count=0)

    stacks: list[str] = []
    for marker, label in _STACK_MARKERS:
        if (root / marker).exists() and label not in stacks:
            stacks.append(label)

    # Only the given root counts: shelling out to git here would happily find a
    # repository further up the tree and report the wrong project.
    is_repo = (root / ".git").exists()
    branch: str | None = None
    has_commits = False
    changed = 0
    if is_repo:
        has_commits = _git(root, "rev-parse", "--verify", "--quiet", "HEAD") is not None
        if has_commits:
            branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
            if branch == "HEAD":  # detached
                branch = None
        status = _git(root, "status", "--porcelain")
        changed = len(status.splitlines()) if status else 0

    instructions_file, instructions = _read_instructions(root)

    return ProjectContext(
        root=root,
        stacks=stacks,
        git_branch=branch,
        git_changed=changed,
        git_is_repo=is_repo,
        git_has_commits=has_commits,
        file_count=_count_files(root),
        instructions_file=instructions_file,
        instructions=instructions,
    )
