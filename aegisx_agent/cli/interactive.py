"""Interactive chat: animated progress, slash dispatch, and streaming output."""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from aegisx_agent.cli.app import console, print_delegation_progress
from aegisx_agent.cli.commands.code import (
    _handle_code_command,
    _handle_git_command,
    _handle_test_command,
)
from aegisx_agent.cli.commands.daemon import _handle_daemon_command
from aegisx_agent.cli.commands.mcp import _handle_mcp_command
from aegisx_agent.cli.commands.observability import (
    _handle_audit_command,
    _handle_usage_command,
)
from aegisx_agent.cli.commands.permissions import (
    _handle_permissions_command,
    _print_tools_table,
)
from aegisx_agent.cli.commands.plugins import _handle_plugin_command
from aegisx_agent.cli.commands.schedule import _handle_schedule_command
from aegisx_agent.llm.base import Message, Role

if TYPE_CHECKING:
    from aegisx_agent.core import AegisXAgent

# ═══════════════════════════════════════════════════
#  ANIMATED PROGRESS
# ═══════════════════════════════════════════════════

THINKING_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
TOOL_ICONS = {
    "calculator": "🔢", "datetime": "📅", "web_search": "🔍",
    "execute_code": "🐍", "file_ops": "📁", "shell": "💻",
    "api_call": "🌐", "db_query": "🗄️", "web_scrape": "🕷️",
    "rag_search": "📚",
}


class AnimatedProgress:
    """Animated spinner that shows thinking/tool status."""

    def __init__(self) -> None:
        self._frame = 0
        self._running = False
        self._message = ""

    def thinking(self, message: str = "🤔 Thinking...") -> None:
        """Show thinking animation."""
        self._message = message
        self._running = True

    def tool_call(self, tool_name: str, status: str = "running") -> None:
        """Show tool call animation."""
        icon = TOOL_ICONS.get(tool_name, "🔧")
        if status == "running":
            self._message = f"{icon} Using {tool_name}..."
        elif status == "success":
            self._message = f"{icon} {tool_name} ✅"
        elif status == "error":
            self._message = f"{icon} {tool_name} ❌"

    def stop(self) -> None:
        self._running = False

    def get_frame(self) -> str:
        if not self._running:
            return ""
        frame = THINKING_FRAMES[self._frame % len(THINKING_FRAMES)]
        self._frame += 1
        return f"  {frame} {self._message}"


class StreamLinePrinter:
    """Write a streamed turn line-by-line without breaking prompts.

    Emits ``🤖 AegisX:`` before the first chunk and dims tool-status lines
    so tool activity reads as a feed. It never runs on a background thread
    and never rewrites a line with ``\r`` — the old spinner did both, which
    overwrote the ``🔐 Approval required`` panel while the permission gate
    waited for an answer, forcing blind y/n input.
    """

    _STATUS_MARKERS = (
        "🔧", "📁", "💻", "🐍", "🔍", "🌐", "🗄️", "🕷️",
        "📚", "✎", "🧪", "🗂️", "📦", "👥", "🔢", "📅",
    )

    def __init__(self) -> None:
        self._started = False
        self._buffer = ""
        self._in_code = False

    def write(self, chunk: str) -> None:
        if not self._started:
            self._started = True
            sys.stdout.write("🤖 AegisX: ")
        if chunk.startswith("\n") and chunk[1:].startswith(self._STATUS_MARKERS):
            # A tool-status line: newline separation + dim on a TTY.
            body = chunk[1:].rstrip("\n")
            if sys.stdout.isatty():
                sys.stdout.write(f"\n\033[2m{body}\033[0m\n")
            else:
                sys.stdout.write(f"\n{body}\n")
            return
        # Markdown blocks (fenced code) are flushed through rich so the code
        # lands with real highlighting; plain prose keeps streaming raw so it
        # stays as live as before.
        if not sys.stdout.isatty():
            sys.stdout.write(chunk)
            sys.stdout.flush()
            return
        self._buffer += chunk
        self._drain_buffer()

    def _find_fence_start(self, buf: str) -> int:
        """Index of the first line-start ``` in ``buf``, or -1."""
        if buf.startswith("```"):
            return 0
        idx = buf.find("\n```")
        return idx + 1 if idx != -1 else -1

    def _drain_buffer(self) -> None:
        """Emit complete fenced blocks via rich; prose streams line-by-line.

        A partial trailing line is held back until it cannot grow into a
        fence marker, so ``` split across chunk boundaries never leaks raw.
        """
        while True:
            if self._in_code:
                close = self._buffer.find("\n```")
                if close == -1:
                    break  # fence still open; wait for more chunks
                block = self._buffer[: close + 4]
                self._buffer = self._buffer[close + 4 :]
                self._flush_markdown(block)
                self._in_code = False
                continue
            start = self._find_fence_start(self._buffer)
            if start == -1:
                break
            prose = self._buffer[:start]
            if prose:
                sys.stdout.write(prose)
            self._buffer = self._buffer[start:]
            if self._buffer.find("\n") == -1:
                break  # opening fence line incomplete (e.g. "```py")
            close = self._buffer.find("\n```")
            if close == -1:
                self._in_code = True
                break
            block = self._buffer[: close + 4]
            self._buffer = self._buffer[close + 4 :]
            self._flush_markdown(block)
        if not self._in_code and self._buffer:
            last_nl = self._buffer.rfind("\n")
            if last_nl != -1:
                sys.stdout.write(self._buffer[: last_nl + 1])
                self._buffer = self._buffer[last_nl + 1 :]
        sys.stdout.flush()

    def _flush_markdown(self, block: str) -> None:
        """Render one completed markdown block through rich."""
        from rich.markdown import Markdown as RichMarkdown

        console.print("\n", end="")
        console.print(RichMarkdown(block.rstrip()))

    def finish(self) -> None:
        if self._started:
            if self._buffer:
                if self._in_code or (
                    self._find_fence_start(self._buffer) == 0 and "\n" in self._buffer
                ):
                    # An unterminated code fence: render what arrived.
                    self._flush_markdown(self._buffer)
                else:
                    sys.stdout.write(self._buffer)
                self._buffer = ""
            sys.stdout.write("\n")
            sys.stdout.flush()


# ═══════════════════════════════════════════════════
#  SLASH COMMANDS
# ═══════════════════════════════════════════════════

COMMANDS = {
    "/help":      {"desc": "Show all commands", "icon": "📖"},
    "/model":     {"desc": "Switch LLM model", "icon": "🧠"},
    "/provider":  {"desc": "Switch LLM provider", "icon": "🔌"},
    "/persona":   {"desc": "Switch agent persona", "icon": "🎭"},
    "/tools":     {"desc": "List available tools", "icon": "🔧"},
    "/skills":    {"desc": "Skills: view/export/import", "icon": "💡"},
    "/code":      {"desc": "Coding mode — analyze/edit/run tests", "icon": "💻"},
    "/git":       {"desc": "Git operations (status/diff/commit)", "icon": "📦"},
    "/test":      {"desc": "Run project tests", "icon": "🧪"},
    "/schedule":  {"desc": "Manage & run scheduled tasks", "icon": "⏰"},
    "/plugin":    {"desc": "List/load/unload tool plugins", "icon": "🧩"},
    "/mcp":       {"desc": "Manage MCP servers and their tools", "icon": "🌐"},
    "/daemon":    {"desc": "Persistent scheduler daemon", "icon": "🛰️"},
    "/usage":     {"desc": "Token usage summary", "icon": "📈"},
    "/audit":     {"desc": "Tool decision audit trail", "icon": "🛡️"},
    "/permissions": {"desc": "Show/change tool permissions", "icon": "🔐"},
    "/sessions":  {"desc": "Session stats & search", "icon": "📊"},
    "/resume":    {"desc": "Continue a previous session", "icon": "⏯️"},
    "/undo":      {"desc": "Drop the last exchange", "icon": "↩️"},
    "/learn":     {"desc": "Teach agent a preference", "icon": "🎓"},
    "/config":    {"desc": "Show current config", "icon": "⚙️"},
    "/clear":     {"desc": "Clear conversation memory", "icon": "🧹"},
    "/ingest":    {"desc": "Ingest document (RAG)", "icon": "📚"},
    "/status":    {"desc": "Show agent status", "icon": "📋"},
    "/quit":      {"desc": "Exit AegisX Agent", "icon": "👋"},
}


#: Slash commands grouped the way people look for them, for /help.
_COMMAND_GROUPS: list[tuple[str, list[str]]] = [
    ("Session", ["/help", "/status", "/config", "/sessions", "/resume", "/undo", "/clear", "/quit"]),  # noqa: E501
    ("Model & behavior", ["/model", "/provider", "/persona", "/learn"]),
    ("Coding", ["/code", "/git", "/test"]),
    ("Knowledge", ["/skills", "/ingest"]),
    ("Automation", ["/schedule", "/daemon", "/plugin", "/mcp"]),
    ("Observability", ["/usage", "/audit", "/permissions"]),
]


def _show_command_menu(filter_text: str = "") -> None:
    """Show / command menu, grouped by purpose."""
    if filter_text:
        # Filtering: a flat list is more useful than groups here.
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Icon", style="bold")
        table.add_column("Command", style="bold magenta")
        table.add_column("Description", style="dim")
        for cmd, info in COMMANDS.items():
            if filter_text.lower() in cmd.lower():
                table.add_row(info["icon"], cmd, info["desc"])
        console.print()
        console.print(Panel(table, title="⌨️  Commands", border_style="magenta", padding=(0, 1)))
        console.print()
        return

    console.print()
    console.print("[bold magenta]⌨️  Commands[/bold magenta]", justify="left")
    for title, cmds in _COMMAND_GROUPS:
        lines = []
        for cmd in cmds:
            info = COMMANDS.get(cmd, {"icon": "🔧", "desc": ""})
            lines.append(
                f"  {info['icon']} [bold magenta]{cmd}[/bold magenta]"
                f"  [dim]{info['desc']}[/dim]"
            )
        body = "\n".join(lines)
        console.print(Panel(body, title=title, border_style="dim", padding=(0, 1)))
    console.print("[dim]Type a command, or just ask the agent in plain language.[/dim]")
    console.print()


def _handle_slash_command(cmd: str, agent: AegisXAgent) -> bool:
    """Handle slash commands. Returns True if handled."""
    parts = cmd.strip().split(maxsplit=1)
    command = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    match command:
        case "/help":
            _show_command_menu()
            return True

        case "/model":
            if args:
                try:
                    agent.set_model(args)
                    console.print(f"[success]✅ Model set to: {args}[/success]")
                except Exception as exc:
                    console.print(f"[error]Could not switch model: {exc}[/error]")
            else:
                info = agent.get_provider_info()
                console.print(f"[info]Current model: {info['model']}[/info]")
                console.print("[dim]Usage: /model <model-name>[/dim]")
            return True

        case "/provider":
            if args:
                try:
                    agent.set_provider(args)
                    console.print(f"[success]✅ Provider set to: {args}[/success]")
                except ValueError as exc:
                    console.print(f"[error]Cannot switch provider: {exc}[/error]")
                    console.print("[dim]Options: openai, anthropic, ollama, groq, custom[/dim]")
            else:
                info = agent.get_provider_info()
                console.print(f"[info]Current provider: {info['provider']}[/info]")
                console.print("[dim]Usage: /provider <name>[/dim]")
            return True

        case "/persona":
            if args:
                try:
                    agent.set_persona(args)
                    console.print(f"[success]✅ Persona: {args}[/success]")
                except Exception:
                    console.print(f"[error]Persona '{args}' not found[/error]")
            else:
                console.print("[info]Available personas:[/info]")
                for p in agent.list_personas():
                    console.print(f"  🎭 {p}")
                console.print("[dim]Usage: /persona <name>[/dim]")
            return True

        case "/tools":
            _print_tools_table(
                agent,
                max_width=55,
                title="🔧 Available Tools",
                risk_filter=args.strip() or None,
            )
            return True

        case "/skills":
            parts = args.split(maxsplit=1) if args else []
            if parts and parts[0] in ("export", "import"):
                sub, rest = parts[0], parts[1].strip() if len(parts) > 1 else ""
                if not rest:
                    console.print(f"[dim]Usage: /skills {sub} <name-or-path> [dest][/dim]")
                    return True
                try:
                    if sub == "export":
                        # Skill names may contain spaces; the destination (if
                        # given) is the last token that looks like a path.
                        tokens = rest.split()
                        dest = None
                        if len(tokens) > 1 and (
                            "/" in tokens[-1] or tokens[-1].endswith((".md", ".json"))
                        ):
                            dest = tokens[-1]
                            name = " ".join(tokens[:-1])
                        else:
                            name = rest
                        dest = dest or f"{name.lower().replace(' ', '_')}.md"
                        path = agent.skill_manager.export_skill(name, dest)
                        console.print(f"[success]📤 Exported '{name}' → {path}[/success]")
                    else:
                        skill = agent.skill_manager.import_skill(rest)
                        console.print(
                            f"[success]📥 Imported '{skill.name}' — "
                            f"{len(skill.steps)} steps[/success]"
                        )
                except (KeyError, FileNotFoundError, ValueError) as exc:
                    console.print(f"[error]Skill {sub} failed: {exc}[/error]")
                return True
            if args:
                content = agent.get_skill(args)
                if content:
                    console.print(
                        Panel(content, title=f"💡 Skill: {args}", border_style="magenta")
                    )
                else:
                    matches = agent.search_skills(args)
                    if matches:
                        console.print(f"[info]No exact skill '{args}'. Closest matches:[/info]")
                        for match in matches[:10]:
                            console.print(f"  💡 {match['name']}: {match['description']}")
                    else:
                        console.print(f"[error]No skill matches '{args}'[/error]")
                return True

            skills = agent.list_skills()
            if not skills:
                console.print(
                    "[dim]No skills learned yet. Skills auto-create after complex tasks.[/dim]"
                )
            else:
                table = Table(title="💡 Learned Skills", border_style="magenta")
                table.add_column("Name", style="bold")
                table.add_column("Description", max_width=50)
                for s in skills:
                    table.add_row(s["name"], s["description"][:50])
                console.print(table)
            return True

        case "/code":
            _handle_code_command(args, agent)
            return True

        case "/git":
            _handle_git_command(args, agent)
            return True

        case "/test":
            _handle_test_command(args, agent)
            return True

        case "/schedule":
            _handle_schedule_command(args, agent)
            return True

        case "/plugin":
            _handle_plugin_command(args, agent)
            return True

        case "/mcp":
            _handle_mcp_command(args, agent)
            return True

        case "/daemon":
            _handle_daemon_command(args, agent)
            return True

        case "/usage":
            _handle_usage_command(args, agent)
            return True

        case "/audit":
            _handle_audit_command(args, agent)
            return True

        case "/permissions":
            _handle_permissions_command(args, agent)
            return True

        case "/sessions":
            stats = agent.get_session_stats()
            console.print(Panel(
                f"Sessions: [cyan]{stats['total_sessions']}[/cyan]"
                f" | Messages: [cyan]{stats['total_messages']}[/cyan]",
                title="📊 Session Stats", border_style="cyan",
            ))
            return True

        case "/undo":
            history = agent.session_store.get_session_history(agent.session_id)
            if len(history) < 2:
                console.print("[warning]Nothing to undo yet.[/warning]")
                return True
            # Drop the last user+assistant pair from memory and the store.
            msgs = agent.conversation.messages
            drop = 0
            if msgs and msgs[-1].role == Role.ASSISTANT:
                drop += 1
                if msgs and len(msgs) >= 2 and msgs[-2].role == Role.USER:
                    drop += 1
            if drop:
                agent.conversation.truncate_to(len(msgs) - drop)
            kept_rows = agent.session_store.trim_session(
                agent.session_id, keep=max(0, len(history) - 2)
            )
            console.print(
                f"[success]↩️  Undid the last exchange "
                f"({drop} message(s), {kept_rows} stored row(s) removed).[/success]"
            )
            return True

        case "/resume":
            if args:
                target = args.strip()
            else:
                previews: list[dict[str, Any]] = []
                store = agent.session_store
                preview_getter = getattr(store, "get_session_previews", None)
                if callable(preview_getter):
                    previews = list(preview_getter(limit=10))
                if previews:
                    table = Table(title="⏯️ Recent sessions", border_style="cyan")
                    table.add_column("#", style="dim", width=2)
                    table.add_column("Session id", style="bold")
                    table.add_column("Msgs", justify="right")
                    table.add_column("Last active", style="dim")
                    table.add_column("Opening message", max_width=48)
                    for i, prev in enumerate(previews, 1):
                        row_id = str(prev.get("session_id", ""))
                        row_msgs = str(prev.get("messages", ""))
                        row_last = str(prev.get("last", ""))[:19]
                        row_first = str(prev.get("first_message", ""))
                        table.add_row(str(i), row_id, row_msgs, row_last, row_first)
                    console.print(table)
                    pick = Prompt.ask("Resume which", default="1")
                    if not pick.isdigit() or not (1 <= int(pick) <= len(previews)):
                        console.print("[warning]Cancelled[/warning]")
                        return True
                    target = str(previews[int(pick) - 1]["session_id"])
                else:
                    sessions = store.get_recent_sessions(limit=10)
                    if not sessions:
                        console.print("[warning]No saved sessions found.[/warning]")
                        return True
                    console.print("[info]Recent sessions:[/info]")
                    for i, sid in enumerate(sessions, 1):
                        console.print(f"  [cyan]{i}[/cyan]. {sid}")
                    pick = Prompt.ask("Resume which", default="1")
                    if not pick.isdigit() or not (1 <= int(pick) <= len(sessions)):
                        console.print("[warning]Cancelled[/warning]")
                        return True
                    target = sessions[int(pick) - 1]
            history = agent.session_store.get_session_history(target)
            agent.conversation.clear()
            agent.conversation.add_messages([
                Message(role=Role.USER if h["role"] == "user" else Role.ASSISTANT,
                        content=h["content"])
                for h in history
            ])
            agent.session_id = target
            console.print(
                f"[success]✅ Resumed session {target} "
                f"({len(history)} messages). Continue where you left off.[/success]"
            )
            return True

        case "/learn":
            if args:
                parts2 = args.split(maxsplit=1)
                if len(parts2) == 2:
                    agent.learn_preference(parts2[0], parts2[1])
                    console.print(f"[success]✅ Learned: {parts2[0]} = {parts2[1]}[/success]")
                else:
                    console.print("[dim]Usage: /learn <key> <value>[/dim]")
            else:
                console.print("[dim]Usage: /learn <key> <value>[/dim]")
                console.print("[dim]Example: /learn language Indonesian[/dim]")
            return True

        case "/config":
            info = agent.get_provider_info()
            table = Table(title="⚙️ Configuration", border_style="cyan")
            table.add_column("Setting", style="bold")
            table.add_column("Value")
            table.add_row("Provider", info["provider"])
            table.add_row("Model", info["model"])
            table.add_row("Tools", str(len(agent.list_tools())))
            table.add_row("Skills", str(len(agent.list_skills())))
            table.add_row("Personas", str(len(agent.list_personas())))
            console.print(table)
            return True

        case "/clear":
            agent.clear_memory()
            console.print("[success]✅ Memory cleared[/success]")
            return True

        case "/ingest":
            if args:
                target_path = Path(args).expanduser()
                if not target_path.exists():
                    console.print(f"[error]Path not found: {args}[/error]")
                elif target_path.is_dir():
                    if agent._rag_engine is None:
                        console.print("[error]RAG is disabled. Enable it in config.[/error]")
                        return True
                    chunks = asyncio.run(agent._rag_engine.ingest_directory(str(target_path)))
                    console.print(f"[success]✅ Ingested {chunks} chunks from {args}[/success]")
                else:
                    chunks = asyncio.run(agent.ingest_document(str(target_path)))
                    console.print(f"[success]✅ Ingested {chunks} chunks from {args}[/success]")
            else:
                console.print("[dim]Usage: /ingest <path-to-file-or-directory>[/dim]")
            return True

        case "/status":
            info = agent.get_provider_info()
            console.print(Panel(
                f"Provider: [cyan]{info['provider']}[/cyan] | Model: [cyan]{info['model']}[/cyan]\n"
                f"Tools: [yellow]{len(agent.list_tools())}[/yellow] | "
                f"Skills: [magenta]{len(agent.list_skills())}[/magenta] | "
                f"Session: [green]{agent.session_id}[/green]",
                title="🤖 AegisX Status", border_style="green",
            ))
            return True

        case "/quit" | "/exit" | "/q":
            console.print("[dim]Goodbye! 👋[/dim]")
            raise SystemExit(0)

    return False


# ═══════════════════════════════════════════════════
#  WORKSPACE
# ═══════════════════════════════════════════════════

def _print_workspace(agent: AegisXAgent) -> None:
    """Show the folder the agent is working in, so its choices make sense."""
    project = agent.get_project()
    if project is None:
        return

    console.print(f"  📁 [bold cyan]{project.name}[/bold cyan] [dim]({project.root})[/dim]")

    details = []
    if project.stacks:
        details.append(", ".join(project.stacks))
    if project.git_is_repo:
        details.append(f"{project.git_label()}, {project.git_dirt_label()}")
    else:
        details.append("not a git repo")
    details.append(f"{project.file_count} files")
    console.print(f"  [dim]{' • '.join(details)}[/dim]")

    if project.instructions_file:
        console.print(f"  📜 [magenta]{project.instructions_file} loaded as instructions[/magenta]")


def _read_piped_prompt() -> str:
    """Read a one-shot task from stdin when nothing was passed as an argument."""
    if sys.stdin is None or sys.stdin.isatty():
        return ""
    try:
        return sys.stdin.read().strip()
    except (OSError, UnicodeDecodeError):
        return ""


# ═══════════════════════════════════════════════════
#  INPUT: prompt_toolkit with graceful fallback
# ═══════════════════════════════════════════════════

def _input_function() -> Callable[[str], str] | None:
    """Return a ``prompt()`` callable with slash-menu + history, or ``None``.

    ``None`` means prompt_toolkit is unavailable or the session is not a
    terminal — the caller then falls back to plain ``Prompt.ask``.
    """
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return None
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import WordCompleter
        from prompt_toolkit.formatted_text import ANSI
        from prompt_toolkit.history import InMemoryHistory
        from prompt_toolkit.styles import Style as PTStyle

        commands = sorted(COMMANDS)
        completer = WordCompleter(commands, sentence=True)
        history: Any = InMemoryHistory()
        pt_style = PTStyle.from_dict({"": "ansibrightblue bold"})
        session: Any = PromptSession(history=history, completer=completer)

        def prompt(message: str = "") -> str:
            label = message.replace("[bold blue]", "").replace("[/bold blue]", "")
            result: str = session.prompt(
                ANSI(f"\x1b[1;34m{label}\x1b[0m "),
                style=pt_style,
            )
            return result

        return prompt
    except Exception:  # noqa: BLE001 — any pt failure falls back to rich Prompt
        return None


def _run_chat(agent: AegisXAgent, no_stream: bool = False) -> None:
    """Main interactive chat loop."""
    info = agent.get_provider_info()

    # Welcome banner
    console.print()
    banner = Text()
    banner.append("  ╔══════════════════════════════════════════════╗\n", style="green")
    banner.append("  ║  🤖 ", style="green")
    banner.append("AEGISX AGENT", style="bold white")
    banner.append(" — Super-powered Agentic AI  ║\n", style="green")
    banner.append("  ╚══════════════════════════════════════════════╝", style="green")
    console.print(banner)
    console.print()

    _print_workspace(agent)

    # Status line — safe display
    persona_name = str(agent.config.persona) if agent.config.persona else "default"
    # Clean up typer artifacts
    if "OptionInfo" in persona_name:
        persona_name = "default"
    console.print(
        f"  🔌 [cyan]{info['provider']}[/cyan] • "
        f"🧠 [cyan]{info['model']}[/cyan] • "
        f"🔧 [yellow]{len(agent.list_tools())} tools[/yellow] • "
        f"💡 [magenta]{len(agent.list_skills())} skills[/magenta] • "
        f"🔐 [yellow]{agent.permission_gate.mode.value}[/yellow] • "
        f"🎭 [green]{persona_name}[/green]"
    )
    console.print(
        '  [dim]Type / for commands • /quit to exit • one-shot: aegisx run "task"[/dim]'
    )
    console.print()

    input_fn = _input_function()

    while True:
        try:
            if input_fn is not None:
                user_input = input_fn("You ❯")
            else:
                user_input = Prompt.ask("[bold blue]You[/bold blue]")
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Goodbye! 👋[/dim]")
            break

        if not user_input.strip():
            continue

        # Handle slash commands
        if user_input.strip().startswith("/"):
            try:
                _handle_slash_command(user_input.strip(), agent)
            except SystemExit:
                break
            continue

        # Regular chat with animation
        try:
            _chat_with_animation(agent, user_input, no_stream)
        except KeyboardInterrupt:
            console.print("\n[warning]Interrupted[/warning]")
        except ValueError as e:
            console.print("\n[error]Configuration Error:[/error]")
            console.print(Panel(str(e), title="❌ Setup Required", border_style="red"))
        except Exception as e:
            _print_chat_error(e)


def _print_chat_error(e: Exception) -> None:
    """Show one chat failure with the server's message and an action hint."""
    import httpx

    text = str(e)
    lowered = text.lower()

    # HTTP status classification applies both to real HTTPStatusError and to
    # wrapped/bubbled exceptions whose message carries the server's text.
    if isinstance(e, httpx.HTTPStatusError) or "http" in lowered or "401" in text:
        if "insufficient" in lowered or "quota" in lowered or "balance" in lowered:
            console.print(Panel(
                f"{text}\n\n[warning]Your API account is out of credit.[/warning] "
                "Top up the account, or switch provider with `aegisx init`.",
                title="💸 Quota exhausted", border_style="red",
            ))
            return
        if "content-blocked" in lowered or "content blocked" in lowered:
            console.print(Panel(
                f"{text}\n\n[warning]The endpoint rejected this request.[/warning] "
                "Some 'router' services fingerprint their clients and only allow "
                "their own app — AegisX cannot bypass that. Try a genuinely "
                "OpenAI-compatible endpoint (OpenRouter, DeepSeek, Groq, Ollama) "
                "via `aegisx init`.",
                title="🚫 Content blocked", border_style="red",
            ))
            return
        if "401" in text or "403" in text or "unauthorized" in lowered:
            console.print(Panel(
                f"{text}\n\n[warning]The key was rejected.[/warning] Check it with "
                "`aegisx config-info`, or set a fresh one via `aegisx init`.",
                title="🔑 Authentication failed", border_style="red",
            ))
            return
        if "429" in text or "rate" in lowered:
            console.print(Panel(
                f"{text}\n\n[dim]Requests are auto-retried with backoff; this one "
                "kept failing. Wait a moment or lower the request rate.[/dim]",
                title="⏳ Rate limited", border_style="yellow",
            ))
            return
    if "unauthorized client" in lowered:
        console.print(Panel(
            f"{text}\n\n[warning]The endpoint only accepts its own client.[/warning] "
            "AegisX is a generic OpenAI-compatible client and cannot impersonate "
            "one. Use a different endpoint.",
            title="🚫 Client not allowed", border_style="red",
        ))
        return
    if "connect" in lowered or "timeout" in lowered:
        console.print(Panel(
            f"{text}\n\n[dim]Cannot reach the LLM provider. Check your connection "
            "or the base URL in `aegisx config-info`.[/dim]",
            title="🌐 Connection error", border_style="red",
        ))
        return
    console.print(Panel(str(e), title="❌ Error", border_style="red"))


def _chat_with_animation(agent: AegisXAgent, user_message: str, no_stream: bool = False) -> None:
    """Chat with live, line-based status — no spinner thread.

    A background spinner used to repaint the line with ``\r`` even while the
    permission gate waited at the approval prompt, so answers were typed
    blind. Tool status now arrives as plain dim lines inside the stream
    instead: each tool call prints what is about to run as it starts, and
    its outcome right after it finishes.
    """
    printer = StreamLinePrinter()
    try:
        if no_stream:
            # No stream to carry tool status or delegation telemetry, so a
            # static notice stands in — nothing repaints the approval panel.
            console.print("[dim]🤔 Thinking…[/dim]")
            response = asyncio.run(
                agent.chat(user_message, on_progress=print_delegation_progress)
            )
            console.print()
            console.print(Panel(Markdown(response), title="🤖 AegisX", border_style="green"))
            console.print()
        else:
            # Streaming mode: ONE request per turn. Tool calls are parsed from
            # the stream itself and announce themselves the moment they start.
            console.print()

            async def _stream() -> None:
                is_tty = sys.stdout.isatty()
                async for chunk in agent.chat_stream(user_message, emit_summary=True):
                    if chunk.startswith("\n⚡"):
                        # Turn summary footer: dim it on a TTY, keep plain
                        # otherwise so captured output stays grep-friendly.
                        printer.write(f"\n\033[2m{chunk[1:]}\033[0m" if is_tty else chunk)
                    else:
                        printer.write(chunk)
                    time.sleep(0.02)  # Smooth typing speed

            asyncio.run(_stream())
            printer.finish()
    except Exception:
        printer.finish()
        raise


# ═══════════════════════════════════════════════════
#  CLI ENTRY POINTS
# ═══════════════════════════════════════════════════
