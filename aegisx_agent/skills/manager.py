"""Skill manager — creates, loads, improves, and manages agent skills.

Inspired by Hermes Agent's closed learning loop:
- Agent completes a task
- If task was complex (5+ tool calls, error recovery, user correction), create a skill
- Skills are stored as markdown files in ~/.aegisx/skills/
- Skills can be improved when the agent finds better approaches
- Progressive disclosure: only skill names loaded by default, full content on demand
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aegisx_agent.skills.skill import Skill

#: A share link, not a file: the HTML gist page and the GitHub blob view both
#: render the skill as a web page, so they need rewriting before downloading.
_GIST_PAGE_RE = re.compile(r"^https?://gist\.github\.com/([^/]+)/([0-9a-fA-F]+)")
_BLOB_PAGE_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/]+)/blob/(.+)$"
)


def normalize_skill_url(url: str) -> str:
    """Turn a pasted share link into a raw file URL.

    ``https://gist.github.com/user/abc123`` → raw gist content, and
    ``https://github.com/o/r/blob/main/skills/x.md`` → its raw counterpart.
    Anything else is returned unchanged, so plain raw URLs keep working.
    """
    candidate = url.strip()
    match = _GIST_PAGE_RE.match(candidate)
    if match:
        user, gist_id = match.groups()
        return f"https://gist.githubusercontent.com/{user}/{gist_id}/raw"
    blob = _BLOB_PAGE_RE.match(candidate)
    if blob:
        owner, repo, rest = blob.groups()
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{rest}"
    return candidate


class SkillManager:
    """Manages the agent's skill library."""

    def __init__(self, skills_dir: str | Path | None = None) -> None:
        if skills_dir:
            self.skills_dir = Path(skills_dir).expanduser()
        else:
            self.skills_dir = Path("~/.aegisx/skills").expanduser()
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self._skills: dict[str, Skill] = {}
        self._load_all()

    def _load_all(self) -> None:
        """Load all skills from disk."""
        for f in self.skills_dir.iterdir():
            if f.is_file() and f.suffix in (".md", ".json"):
                try:
                    skill = Skill.from_file(f)
                    self._skills[skill.name] = skill
                except Exception:
                    pass

    def get(self, name: str) -> Skill | None:
        """Get a skill by name."""
        return self._skills.get(name)

    def list_skills(self) -> list[Skill]:
        """List all skills."""
        return list(self._skills.values())

    def list_summaries(self) -> list[dict[str, str]]:
        """List skill names and descriptions (for progressive disclosure)."""
        return [
            {"name": s.name, "description": s.description, "category": s.category}
            for s in self._skills.values()
        ]

    def search(self, query: str) -> list[Skill]:
        """Search skills by name, description, or tags."""
        query_lower = query.lower()
        results = []
        for skill in self._skills.values():
            if (
                query_lower in skill.name.lower()
                or query_lower in skill.description.lower()
                or any(query_lower in tag.lower() for tag in skill.tags)
            ):
                results.append(skill)
        return sorted(results, key=lambda s: s.use_count, reverse=True)

    def create_skill(
        self,
        name: str,
        description: str,
        steps: list[str],
        tool_calls: list[dict[str, Any]] | None = None,
        tags: list[str] | None = None,
        category: str = "general",
    ) -> Skill:
        """Create a new skill."""
        # Sanitize name for filename
        safe_name = self._sanitize(name)
        skill = Skill(
            name=name,
            description=description,
            steps=steps,
            tool_calls=tool_calls or [],
            tags=tags or [],
            category=category,
        )
        self._skills[name] = skill
        skill.save(self.skills_dir / f"{safe_name}.md")
        return skill

    def update_skill(
        self,
        name: str,
        steps: list[str] | None = None,
        description: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> Skill | None:
        """Update an existing skill (patch, not rewrite)."""
        skill = self._skills.get(name)
        if not skill:
            return None

        if steps is not None:
            skill.steps = steps
        if description is not None:
            skill.description = description
        if tool_calls is not None:
            skill.tool_calls = tool_calls

        from datetime import datetime
        skill.updated_at = datetime.now().isoformat()
        skill.version = self._bump_version(skill.version)

        safe_name = self._sanitize(name)
        skill.save(self.skills_dir / f"{safe_name}.md")
        return skill

    def delete_skill(self, name: str) -> bool:
        """Delete a skill."""
        if name not in self._skills:
            return False
        safe_name = self._sanitize(name)
        for ext in [".md", ".json"]:
            path = self.skills_dir / f"{safe_name}{ext}"
            if path.exists():
                path.unlink()
        del self._skills[name]
        return True

    def record_use(self, name: str, success: bool) -> bool:
        """Record a skill usage and persist the updated statistics."""
        skill = self._skills.get(name)
        if skill is None:
            return False
        skill.record_use(success)
        skill.save(self.skills_dir / f"{self._sanitize(name)}.md")
        return True

    def export_skill(self, name: str, dest: str | Path) -> Path:
        """Write a skill's markdown to ``dest`` so it can be shared.

        Raises ``KeyError`` when the skill does not exist.
        """
        skill = self._skills.get(name)
        if skill is None:
            raise KeyError(f"No skill named '{name}'")
        dest_path = Path(dest).expanduser()
        if dest_path.suffix not in (".md", ".json"):
            dest_path = dest_path.with_suffix(".md")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        skill.save(dest_path)
        return dest_path

    def import_skill(self, source: str | Path) -> Skill:
        """Load a skill from a shared ``.md``/``.json`` file into the library.

        Returns the imported skill (already persisted into ``skills_dir``).
        Raises ``FileNotFoundError`` for a missing file and ``ValueError``
        when the file does not parse as a skill.
        """
        source_path = Path(source).expanduser()
        if not source_path.is_file():
            raise FileNotFoundError(f"Skill file not found: {source}")
        return self.import_skill_text(
            source_path.read_text(encoding="utf-8"), source=str(source)
        )

    def import_skill_text(self, content: str, source: str = "<inline>") -> Skill:
        """Parse skill content (JSON object or markdown) and persist it.

        Raises ``ValueError`` when the text carries no skill name.
        """
        text = content.strip()
        if not text:
            raise ValueError(f"Skill source is empty: {source}")
        if text.startswith("{"):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as error:
                raise ValueError(f"Skill source is not valid JSON ({source}): {error}") from error
            if not isinstance(payload, dict):
                raise ValueError(f"Skill JSON must be an object: {source}")
            skill = Skill.from_dict(payload)
        else:
            skill = Skill.from_markdown(text)
        if not skill.name:
            raise ValueError(
                f"Skill source has no name — expected a '# Skill: <name>' header: {source}"
            )
        self._skills[skill.name] = skill
        skill.save(self.skills_dir / f"{self._sanitize(skill.name)}.md")
        return skill

    def import_skill_url(self, url: str, fetch: Callable[[str], str]) -> Skill:
        """Import a skill from a URL, downloading through ``fetch``.

        ``fetch`` is injected so the network stays in the CLI layer and the
        parsing/persistence path stays unit-testable.
        """
        raw_url = normalize_skill_url(url)
        content = fetch(raw_url)
        return self.import_skill_text(content, source=url)

    @staticmethod
    def _sanitize(name: str) -> str:
        """Turn a skill name into a safe file stem."""
        return re.sub(r"[^a-z0-9_-]", "_", name.lower().replace(" ", "_"))

    def should_create_skill(
        self,
        tool_call_count: int,
        had_error_recovery: bool,
        had_user_correction: bool,
        workflow_complexity: str = "normal",
    ) -> bool:
        """Determine if a skill should be created from a completed task.

        Triggers (same as Hermes):
        - 5+ tool calls
        - Recovery from error
        - User correction
        - Non-obvious workflow
        """
        if tool_call_count >= 5:
            return True
        if had_error_recovery:
            return True
        if had_user_correction:
            return True
        if workflow_complexity == "complex":
            return True
        return False

    @staticmethod
    def _bump_version(version: str) -> str:
        """Bump patch version."""
        parts = version.split(".")
        if len(parts) == 3:
            parts[2] = str(int(parts[2]) + 1)
        return ".".join(parts)
