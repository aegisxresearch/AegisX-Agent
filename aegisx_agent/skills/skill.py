"""Skill data model — reusable instruction sets the agent creates and improves."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


def _first_int(line: str) -> int:
    """Return the first integer found in a metadata line, or 0."""
    match = re.search(r"\d+", line)
    return int(match.group(0)) if match else 0


@dataclass
class Skill:
    """A reusable skill that the agent has learned.

    Skills are created after successful task completion and can be
    improved over time as the agent finds better approaches.
    """

    name: str
    description: str
    steps: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    category: str = "general"
    version: str = "1.0.0"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    use_count: int = 0
    success_count: int = 0
    last_used: str = ""

    @property
    def success_rate(self) -> float:
        if self.use_count == 0:
            return 0.0
        return self.success_count / self.use_count

    def record_use(self, success: bool) -> None:
        """Record a skill usage."""
        self.use_count += 1
        if success:
            self.success_count += 1
        self.last_used = datetime.now().isoformat()

    def to_markdown(self) -> str:
        """Convert skill to markdown format for storage."""
        lines = [
            f"# Skill: {self.name}",
            "",
            f"**Description:** {self.description}",
            f"**Category:** {self.category}",
            f"**Tags:** {', '.join(self.tags)}",
            f"**Version:** {self.version}",
            f"**Created:** {self.created_at}",
            f"**Updated:** {self.updated_at}",
            f"**Used:** {self.use_count} times ({self.success_rate:.0%} success)",
            f"**Success:** {self.success_count}",
            "",
            "## Steps",
            "",
        ]
        for i, step in enumerate(self.steps, 1):
            lines.append(f"{i}. {step}")

        if self.tool_calls:
            lines.extend(["", "## Tool Calls", ""])
            for tc in self.tool_calls:
                lines.append(f"- `{tc.get('name', 'unknown')}`: {json.dumps(tc.get('args', {}))}")

        return "\n".join(lines)

    def to_prompt(self) -> str:
        """Convert skill to a prompt the agent can follow."""
        lines = [f"Skill: {self.name} — {self.description}", ""]
        for i, step in enumerate(self.steps, 1):
            lines.append(f"Step {i}: {step}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "tags": self.tags,
            "category": self.category,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "use_count": self.use_count,
            "success_count": self.success_count,
            "last_used": self.last_used,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Skill:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    @classmethod
    def from_markdown(cls, content: str) -> Skill:
        """Parse a skill from markdown format."""
        lines = content.strip().split("\n")
        name = ""
        description = ""
        steps: list[str] = []
        tags: list[str] = []
        category = "general"
        version = "1.0.0"
        created_at = ""
        updated_at = ""
        use_count = 0
        success_count = 0

        for line in lines:
            line = line.strip()
            if line.startswith("# Skill:"):
                name = line.replace("# Skill:", "").strip()
            elif line.startswith("**Description:**"):
                description = line.replace("**Description:**", "").strip()
            elif line.startswith("**Category:**"):
                category = line.replace("**Category:**", "").strip()
            elif line.startswith("**Tags:**"):
                raw_tags = line.replace("**Tags:**", "")
                tags = [tag.strip() for tag in raw_tags.split(",") if tag.strip()]
            elif line.startswith("**Version:**"):
                version = line.replace("**Version:**", "").strip() or version
            elif line.startswith("**Created:**"):
                created_at = line.replace("**Created:**", "").strip()
            elif line.startswith("**Updated:**"):
                updated_at = line.replace("**Updated:**", "").strip()
            elif line.startswith("**Used:**"):
                use_count = _first_int(line)
            elif line.startswith("**Success:**"):
                success_count = _first_int(line)
            elif line and line[0].isdigit() and ". " in line:
                step = line.split(". ", 1)[1]
                steps.append(step)

        now = datetime.now().isoformat()
        return cls(
            name=name,
            description=description,
            steps=steps,
            tags=tags,
            category=category,
            version=version,
            created_at=created_at or now,
            updated_at=updated_at or now,
            use_count=use_count,
            success_count=success_count,
        )

    @classmethod
    def from_file(cls, path: Path) -> Skill:
        """Load skill from a file."""
        content = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            return cls.from_dict(json.loads(content))
        return cls.from_markdown(content)

    def save(self, path: Path) -> None:
        """Save skill to a file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".json":
            path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False))
        else:
            path.write_text(self.to_markdown())
