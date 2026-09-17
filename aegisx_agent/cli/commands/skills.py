"""Skill library commands: list, show, export, import (file or URL), search.

Both front ends share these helpers: ``aegisx skills …`` on the command line
and ``/skills …`` inside the chat. Importing understands share links — a gist
page URL is rewritten to its raw content before downloading.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from rich.table import Table

from aegisx_agent.cli.app import console

if TYPE_CHECKING:
    from aegisx_agent.core import AegisXAgent
    from aegisx_agent.skills.skill import Skill

SKILLS_USAGE = """[bold]Usage:[/bold]
  /skills                     — list learned skills
  /skills show <name>         — print one skill in full
  /skills export <name> [dest]— write a skill to a shareable .md file
  /skills import <path|url>   — import from a file, gist, or raw URL
  /skills search <query>      — find skills by keyword
  /skills search <url>        — download and import a skill from a URL"""

#: Long enough for a big skill, short enough that a hung host cannot stall the
#: chat; skills are small markdown documents.
FETCH_TIMEOUT_SECONDS = 20.0
_FETCH_HEADERS = {"User-Agent": "aegisx-agent (skill import)"}


class SkillImportError(RuntimeError):
    """Raised when a remote skill could not be downloaded."""


def _looks_like_url(value: str) -> bool:
    return value.strip().lower().startswith(("http://", "https://"))


def _fetch_text(url: str) -> str:
    """Download a skill file. Raises :class:`SkillImportError` on failure."""
    try:
        response = httpx.get(
            url,
            timeout=FETCH_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers=_FETCH_HEADERS,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise SkillImportError(
            f"HTTP {exc.response.status_code} fetching {url}"
        ) from exc
    except httpx.HTTPError as exc:
        raise SkillImportError(f"Could not fetch {url}: {exc}") from exc
    if not response.text.strip():
        raise SkillImportError(f"Remote skill is empty: {url}")
    return response.text


def import_skill_from_source(agent: AegisXAgent, source: str) -> Skill:
    """Import a skill from a local path or a URL.

    Returns the imported :class:`~aegisx_agent.skills.skill.Skill`.
    """
    if _looks_like_url(source):
        return agent.skill_manager.import_skill_url(source, _fetch_text)
    return agent.skill_manager.import_skill(source)


def _print_skills_table(agent: AegisXAgent, title: str = "💡 Learned Skills") -> None:
    skills = agent.list_skills()
    if not skills:
        console.print(
            "[dim]No skills learned yet. Skills auto-create after complex tasks, "
            "or import one with `aegisx skills import <url>`.[/dim]"
        )
        return
    table = Table(title=title, border_style="magenta")
    table.add_column("Name", style="bold")
    table.add_column("Description", max_width=52)
    table.add_column("Category", style="dim")
    for skill in skills:
        table.add_row(
            skill["name"],
            (skill.get("description") or "")[:52],
            skill.get("category") or "general",
        )
    console.print(table)


def _split_name_and_dest(rest: str) -> tuple[str, str | None]:
    """Skill names may contain spaces; a trailing path is the destination."""
    tokens = rest.split()
    if len(tokens) > 1 and (
        "/" in tokens[-1] or tokens[-1].endswith((".md", ".json"))
    ):
        return " ".join(tokens[:-1]), tokens[-1]
    return rest, None


def _export_skill(agent: AegisXAgent, rest: str) -> None:
    name, dest = _split_name_and_dest(rest)
    dest = dest or f"{name.lower().replace(' ', '_')}.md"
    path = agent.skill_manager.export_skill(name, dest)
    console.print(f"[success]📤 Exported '{name}' → {path}[/success]")


def _import_skill(agent: AegisXAgent, rest: str) -> None:
    skill = import_skill_from_source(agent, rest)
    console.print(
        f"[success]📥 Imported '{skill.name}' — {len(skill.steps)} steps[/success]"
    )
    if skill.description:
        console.print(f"[dim]{skill.description}[/dim]")


def _search_skills(agent: AegisXAgent, query: str) -> None:
    """A URL here is a shortcut for import; anything else searches the library."""
    if _looks_like_url(query):
        _import_skill(agent, query)
        return
    matches = agent.search_skills(query)
    if not matches:
        console.print(f"[error]No skill matches '{query}'[/error]")
        return
    table = Table(title=f"🔎 Skills matching '{query}'", border_style="magenta")
    table.add_column("Name", style="bold")
    table.add_column("Description", max_width=52)
    for match in matches[:20]:
        table.add_row(match["name"], (match.get("description") or "")[:52])
    console.print(table)


def _show_skill(agent: AegisXAgent, name: str) -> None:
    content = agent.get_skill(name)
    if content:
        from rich.panel import Panel

        console.print(Panel(content, title=f"💡 Skill: {name}", border_style="magenta"))
        return
    matches = agent.search_skills(name)
    if not matches:
        console.print(f"[error]No skill matches '{name}'[/error]")
        return
    console.print(f"[info]No exact skill '{name}'. Closest matches:[/info]")
    for match in matches[:10]:
        console.print(f"  💡 {match['name']}: {match['description']}")


def _handle_skills_command(args: str, agent: AegisXAgent) -> None:
    """Dispatch ``/skills <subcommand>``."""
    parts = args.strip().split(maxsplit=1)
    subcommand = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if subcommand in ("", "list"):
        _print_skills_table(agent)
        return
    if subcommand == "help":
        console.print(SKILLS_USAGE)
        return
    if subcommand in ("show", "view"):
        if not rest:
            console.print("[error]Usage: /skills show <name>[/error]")
            return
        _show_skill(agent, rest)
        return
    if subcommand == "export":
        if not rest:
            console.print("[error]Usage: /skills export <name> [dest][/error]")
            return
        try:
            _export_skill(agent, rest)
        except KeyError as exc:
            console.print(f"[error]Skill export failed: {exc}[/error]")
        return
    if subcommand == "import":
        if not rest:
            console.print("[error]Usage: /skills import <path|url>[/error]")
            return
        try:
            _import_skill(agent, rest)
        except (FileNotFoundError, ValueError, SkillImportError) as exc:
            console.print(f"[error]Skill import failed: {exc}[/error]")
        return
    if subcommand == "search":
        if not rest:
            console.print("[error]Usage: /skills search <query|url>[/error]")
            return
        try:
            _search_skills(agent, rest)
        except (FileNotFoundError, ValueError, SkillImportError) as exc:
            console.print(f"[error]Skill import failed: {exc}[/error]")
        return

    # Bare argument: treat it as a skill name.
    _show_skill(agent, args.strip())


__all__ = [
    "FETCH_TIMEOUT_SECONDS",
    "SKILLS_USAGE",
    "SkillImportError",
    "_fetch_text",
    "_handle_skills_command",
    "_print_skills_table",
    "import_skill_from_source",
]
