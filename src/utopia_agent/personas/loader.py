"""Persona loader — load custom personas from files or built-in presets."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from utopia_agent.config import DEFAULT_PERSONAS


class PersonaLoader:
    """Load and manage agent personas."""

    def __init__(self, personas_dir: str | Path | None = None) -> None:
        self.personas_dir = Path(personas_dir).expanduser() if personas_dir else None

    def get(self, name: str) -> str:
        """Get a persona by name. Checks built-in first, then custom files."""
        # Check built-in personas
        if name in DEFAULT_PERSONAS:
            return DEFAULT_PERSONAS[name]

        # Check custom persona files
        if self.personas_dir and self.personas_dir.exists():
            for ext in [".txt", ".md", ".prompt"]:
                persona_file = self.personas_dir / f"{name}{ext}"
                if persona_file.exists():
                    return persona_file.read_text(encoding="utf-8").strip()

        # Fallback to default
        return DEFAULT_PERSONAS.get("default", "You are a helpful AI assistant.")

    def list_available(self) -> list[str]:
        """List all available persona names."""
        names = list(DEFAULT_PERSONAS.keys())

        if self.personas_dir and self.personas_dir.exists():
            for f in self.personas_dir.iterdir():
                if f.is_file() and f.suffix in (".txt", ".md", ".prompt"):
                    name = f.stem
                    if name not in names:
                        names.append(name)

        return names

    def save(self, name: str, prompt: str) -> Path:
        """Save a custom persona to disk."""
        if not self.personas_dir:
            self.personas_dir = Path("~/.utopia/personas").expanduser()

        self.personas_dir.mkdir(parents=True, exist_ok=True)
        persona_file = self.personas_dir / f"{name}.txt"
        persona_file.write_text(prompt, encoding="utf-8")
        return persona_file

    def delete(self, name: str) -> bool:
        """Delete a custom persona."""
        if name in DEFAULT_PERSONAS:
            return False  # Can't delete built-in personas

        if self.personas_dir and self.personas_dir.exists():
            for ext in [".txt", ".md", ".prompt"]:
                persona_file = self.personas_dir / f"{name}{ext}"
                if persona_file.exists():
                    persona_file.unlink()
                    return True
        return False

    def get_all_metadata(self) -> list[dict[str, Any]]:
        """Get metadata for all personas."""
        result = []
        for name in self.list_available():
            result.append({
                "name": name,
                "source": "built-in" if name in DEFAULT_PERSONAS else "custom",
                "preview": self.get(name)[:100] + "...",
            })
        return result
