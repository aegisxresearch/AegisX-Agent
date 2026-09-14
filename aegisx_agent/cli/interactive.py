"""Interactive chat: animated progress, slash dispatch, and streaming output."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from aegisx_agent.cli.app import console
from aegisx_agent.cli.commands.code import (
    _handle_code_command,
    _handle_git_command,
    _handle_test_command,
)
from aegisx_agent.cli.commands.mcp import _handle_mcp_command
from aegisx_agent.cli.commands.permissions import (
    _handle_permissions_command,
    _print_tools_table,
)
from aegisx_agent.cli.commands.plugins import _handle_plugin_command
from aegisx_agent.cli.commands.schedule import _handle_schedule_command

if TYPE_CHECKING:
    from aegisx_agent.core import AegisXAgent

# ═══════════════════════════════════════════════════
#  ANIMATED PROGRESS
# ═══════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════
#  SLASH COMMANDS
# ═══════════════════════════════════════════════════

# ═══════════════════════════════════════════════════
#  SLASH COMMANDS
# ═══════════════════════════════════════════════════

COMMANDS = {
    "/help":      {"desc": "Show all commands", "icon": "📖"},
    "/model":     {"desc": "Switch LLM model", "icon": "🧠"},
    "/provider":  {"desc": "Switch LLM provider", "icon": "🔌"},
    "/persona":   {"desc": "Switch agent persona", "icon": "🎭"},
    "/tools":     {"desc": "List available tools", "icon": "🔧"},
    "/skills":    {"desc": "List or load learned skills", "icon": "💡"},
    "/code":      {"desc": "Coding mode — analyze/edit/run tests", "icon": "💻"},
    "/git":       {"desc": "Git operations (status/diff/commit)", "icon": "📦"},
    "/test":      {"desc": "Run project tests", "icon": "🧪"},
    "/schedule":  {"desc": "Manage & run scheduled tasks", "icon": "⏰"},
    "/plugin":    {"desc": "List/load/unload tool plugins", "icon": "🧩"},
    "/mcp":       {"desc": "Manage MCP servers and their tools", "icon": "🌐"},
    "/permissions": {"desc": "Show/change tool permissions", "icon": "🔐"},
    "/sessions":  {"desc": "Session stats & search", "icon": "📊"},
    "/learn":     {"desc": "Teach agent a preference", "icon": "🎓"},
    "/config":    {"desc": "Show current config", "icon": "⚙️"},
    "/clear":     {"desc": "Clear conversation memory", "icon": "🧹"},
    "/ingest":    {"desc": "Ingest document (RAG)", "icon": "📚"},
    "/status":    {"desc": "Show agent status", "icon": "📋"},
    "/quit":      {"desc": "Exit AegisX Agent", "icon": "👋"},
}


def _show_command_menu(filter_text: str = "") -> None:
    """Show / command menu."""
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Icon", style="bold")
    table.add_column("Command", style="bold magenta")
    table.add_column("Description", style="dim")

    for cmd, info in COMMANDS.items():
        if not filter_text or filter_text.lower() in cmd.lower():
            table.add_row(info["icon"], cmd, info["desc"])

    console.print()
    console.print(Panel(table, title="⌨️  Commands", border_style="magenta", padding=(0, 1)))
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
            _print_tools_table(agent, max_width=55, title="🔧 Available Tools")
            return True

        case "/skills":
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
#  MAIN CHAT LOOP
# ═══════════════════════════════════════════════════

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

    while True:
        try:
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
            error_msg = str(e)
            if "Bearer" in error_msg or "401" in error_msg:
                console.print("\n[error]Authentication Failed — API key invalid or missing[/error]")
                console.print("[dim]Use: /provider or set env vars[/dim]")
            elif "connect" in error_msg.lower() or "timeout" in error_msg.lower():
                console.print("\n[error]Connection Error — cannot reach LLM provider[/error]")
            else:
                console.print(f"\n[error]Error: {e}[/error]")


def _chat_with_animation(agent: AegisXAgent, user_message: str, no_stream: bool = False) -> None:
    """Chat with animated thinking/tool progress + streaming."""
    progress = AnimatedProgress()

    def _animate() -> None:
        while progress._running:
            frame = progress.get_frame()
            if frame:
                sys.stdout.write(f"\r{frame}   ")
                sys.stdout.flush()
            time.sleep(0.1)
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()

    import threading

    # Start thinking animation
    progress.thinking("🤔 Thinking...")
    anim_thread = threading.Thread(target=_animate, daemon=True)
    anim_thread.start()

    try:
        if no_stream:
            response = asyncio.run(agent.chat(user_message))
            progress.stop()
            time.sleep(0.15)
            console.print()
            console.print(Panel(Markdown(response), title="🤖 AegisX", border_style="green"))
            console.print()
        else:
            # Streaming mode: ONE request per turn. Tool calls are parsed from
            # the stream itself, so there is no separate probe request — and
            # tool turns stream live instead of falling back to a panel.
            progress.stop()
            time.sleep(0.1)
            console.print()
            console.print("[bold green]🤖 AegisX:[/bold green] ", end="")

            async def _stream() -> None:
                async for chunk in agent.chat_stream(user_message):
                    # Typewriter effect (tool status lines included)
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                    time.sleep(0.02)  # Smooth typing speed

            asyncio.run(_stream())
            console.print("\n")
    except Exception:
        progress.stop()
        time.sleep(0.1)
        raise


# ═══════════════════════════════════════════════════
#  CLI ENTRY POINTS
# ═══════════════════════════════════════════════════
