"""Utopia Agent CLI — Interactive chat with /commands and animated progress."""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.prompt import Prompt
from rich.table import Table
from rich.theme import Theme
from rich.live import Live
from rich.text import Text
from rich.spinner import Spinner

# Rich theme
THEME = Theme({
    "info": "cyan",
    "success": "green bold",
    "warning": "yellow",
    "error": "bold red",
    "user": "bold blue",
    "agent": "bold green",
    "tool": "bold yellow",
    "cmd": "bold magenta",
    "dim": "dim",
})

console = Console(theme=THEME)
app = typer.Typer(name="utopia", help="🤖 Utopia Agent — Super-powered Agentic AI", rich_markup_mode="rich")

# Global agent
_agent = None


def _get_config(provider=None, model=None, api_key=None, custom_url=None):
    from utopia_agent.config import AgentConfig
    config = AgentConfig()
    if provider:
        config.llm_provider = provider
    if model:
        match config.llm_provider.value:
            case "openai": config.openai_model = model
            case "anthropic": config.anthropic_model = model
            case "ollama": config.ollama_model = model
            case "groq": config.groq_model = model
            case "custom": config.custom_model = model
    if api_key:
        match config.llm_provider.value:
            case "openai": config.openai_api_key = api_key
            case "anthropic": config.anthropic_api_key = api_key
            case "groq": config.groq_api_key = api_key
            case "custom": config.custom_api_key = api_key
    if custom_url:
        config.custom_base_url = custom_url
    return config


def _get_agent(config=None):
    global _agent
    if _agent is None:
        from utopia_agent.core import UtopiaAgent
        try:
            _agent = UtopiaAgent(config)
        except ValueError as e:
            # Auto setup wizard
            console.print()
            console.print(Panel(
                "[bold white]Welcome to Utopia Agent![/bold white]\n\n"
                "Let's set up your AI provider.",
                title="🚀 First Time Setup", border_style="green",
            ))
            console.print()

            config = _run_setup_wizard(config)
            _agent = UtopiaAgent(config)
    return _agent


def _run_setup_wizard(config):
    """Interactive setup wizard for first-time users."""
    from utopia_agent.config import LLMProvider

    console.print("[bold]Choose your AI provider:[/bold]")
    console.print("  [cyan]1[/cyan] OpenRouter (recommended — access all models)")
    console.print("  [cyan]2[/cyan] OpenAI")
    console.print("  [cyan]3[/cyan] Anthropic (Claude)")
    console.print("  [cyan]4[/cyan] Ollama (local, free)")
    console.print("  [cyan]5[/cyan] Groq (fast)")
    console.print("  [cyan]6[/cyan] Custom endpoint")
    console.print()

    choice = Prompt.ask("Your choice", default="1")

    match choice:
        case "1":
            config.llm_provider = LLMProvider.CUSTOM
            config.custom_base_url = Prompt.ask(
                "Endpoint URL", default="https://openrouter.ai/api/v1"
            )
            config.custom_api_key = Prompt.ask("API Key", password=True)
            config.custom_model = Prompt.ask(
                "Model", default="anthropic/claude-3.5-sonnet"
            )

        case "2":
            config.llm_provider = LLMProvider.OPENAI
            config.openai_api_key = Prompt.ask("API Key", password=True)
            config.openai_model = Prompt.ask("Model", default="gpt-4o")

        case "3":
            config.llm_provider = LLMProvider.ANTHROPIC
            config.anthropic_api_key = Prompt.ask("API Key", password=True)
            config.anthropic_model = Prompt.ask("Model", default="claude-sonnet-4-20250514")

        case "4":
            config.llm_provider = LLMProvider.OLLAMA
            config.ollama_base_url = Prompt.ask("Ollama URL", default="http://localhost:11434")
            config.ollama_model = Prompt.ask("Model", default="llama3.1")

        case "5":
            config.llm_provider = LLMProvider.GROQ
            config.groq_api_key = Prompt.ask("API Key", password=True)
            config.groq_model = Prompt.ask("Model", default="llama-3.1-70b-versatile")

        case "6":
            config.llm_provider = LLMProvider.CUSTOM
            config.custom_base_url = Prompt.ask("Endpoint URL")
            config.custom_api_key = Prompt.ask("API Key", password=True)
            config.custom_model = Prompt.ask("Model", default="default")

        case _:
            console.print("[error]Invalid choice, using OpenRouter default[/error]")
            config.llm_provider = LLMProvider.CUSTOM
            config.custom_base_url = "https://openrouter.ai/api/v1"
            config.custom_model = "anthropic/claude-3.5-sonnet"

    console.print()
    console.print("[success]✅ Configuration saved![/success]")
    console.print()
    return config


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

    def __init__(self):
        self._frame = 0
        self._running = False
        self._message = ""

    def thinking(self, message="🤔 Thinking..."):
        """Show thinking animation."""
        self._message = message
        self._running = True

    def tool_call(self, tool_name: str, status: str = "running"):
        """Show tool call animation."""
        icon = TOOL_ICONS.get(tool_name, "🔧")
        if status == "running":
            self._message = f"{icon} Using {tool_name}..."
        elif status == "success":
            self._message = f"{icon} {tool_name} ✅"
        elif status == "error":
            self._message = f"{icon} {tool_name} ❌"

    def stop(self):
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

COMMANDS = {
    "/help":      {"desc": "Show all commands", "icon": "📖"},
    "/model":     {"desc": "Switch LLM model", "icon": "🧠"},
    "/provider":  {"desc": "Switch LLM provider", "icon": "🔌"},
    "/persona":   {"desc": "Switch agent persona", "icon": "🎭"},
    "/tools":     {"desc": "List available tools", "icon": "🔧"},
    "/skills":    {"desc": "List learned skills", "icon": "💡"},
    "/code":      {"desc": "Coding mode — analyze/edit/run tests", "icon": "💻"},
    "/git":       {"desc": "Git operations (status/diff/commit)", "icon": "📦"},
    "/test":      {"desc": "Run project tests", "icon": "🧪"},
    "/schedule":  {"desc": "Manage scheduled tasks", "icon": "⏰"},
    "/sessions":  {"desc": "Session stats & search", "icon": "📊"},
    "/learn":     {"desc": "Teach agent a preference", "icon": "🎓"},
    "/config":    {"desc": "Show current config", "icon": "⚙️"},
    "/clear":     {"desc": "Clear conversation memory", "icon": "🧹"},
    "/ingest":    {"desc": "Ingest document (RAG)", "icon": "📚"},
    "/status":    {"desc": "Show agent status", "icon": "📋"},
    "/quit":      {"desc": "Exit Utopia Agent", "icon": "👋"},
}


def _show_command_menu(filter_text: str = ""):
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


def _handle_slash_command(cmd: str, agent) -> bool:
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
                agent.config.custom_model = args if agent.config.llm_provider.value == "custom" else args
                console.print(f"[success]✅ Model set to: {args}[/success]")
            else:
                info = agent.get_provider_info()
                console.print(f"[info]Current model: {info['model']}[/info]")
                console.print("[dim]Usage: /model <model-name>[/dim]")
            return True

        case "/provider":
            if args:
                from utopia_agent.config import LLMProvider
                try:
                    agent.config.llm_provider = LLMProvider(args)
                    console.print(f"[success]✅ Provider set to: {args}[/success]")
                except ValueError:
                    console.print(f"[error]Unknown provider: {args}[/error]")
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
            table = Table(title="🔧 Available Tools", border_style="yellow")
            table.add_column("Tool", style="bold")
            table.add_column("Description", max_width=55)
            for tool in agent.tools.list_tools():
                table.add_row(tool.name, tool.description[:70])
            console.print(table)
            return True

        case "/skills":
            skills = agent.list_skills()
            if not skills:
                console.print("[dim]No skills learned yet. Skills auto-create after complex tasks.[/dim]")
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

        case "/sessions":
            stats = agent.get_session_stats()
            console.print(Panel(
                f"Sessions: [cyan]{stats['total_sessions']}[/cyan] | Messages: [cyan]{stats['total_messages']}[/cyan]",
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
                from pathlib import Path
                p = Path(args).expanduser()
                if not p.exists():
                    console.print(f"[error]Path not found: {args}[/error]")
                elif p.is_dir():
                    chunks = asyncio.run(agent._rag_engine.ingest_directory(str(p)))
                    console.print(f"[success]✅ Ingested {chunks} chunks from {args}[/success]")
                else:
                    chunks = asyncio.run(agent.ingest_document(str(p)))
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
                title="🤖 Utopia Status", border_style="green",
            ))
            return True

        case "/quit" | "/exit" | "/q":
            console.print("[dim]Goodbye! 👋[/dim]")
            raise SystemExit(0)

    return False


def _handle_code_command(args: str, agent):
    """Handle /code subcommands."""
    parts = args.split(maxsplit=1)
    subcmd = parts[0] if parts else ""
    arg = parts[1] if len(parts) > 1 else "."

    match subcmd:
        case "structure" | "tree":
            result = asyncio.run(agent.tools.execute("codebase", {"action": "structure", "path": arg}))
            console.print(result.output)
        case "find":
            result = asyncio.run(agent.tools.execute("codebase", {"action": "find", "path": ".", "query": arg}))
            console.print(result.output)
        case "search":
            result = asyncio.run(agent.tools.execute("codebase", {"action": "search", "path": ".", "query": arg}))
            console.print(result.output)
        case "read":
            result = asyncio.run(agent.tools.execute("codebase", {"action": "read", "path": arg}))
            console.print(result.output)
        case "deps":
            result = asyncio.run(agent.tools.execute("codebase", {"action": "deps", "path": arg}))
            console.print(result.output)
        case "summary":
            result = asyncio.run(agent.tools.execute("codebase", {"action": "summary", "path": arg}))
            console.print(result.output)
        case _:
            console.print("[dim]Usage:[/dim]")
            console.print("  /code structure [path]     — Show project tree")
            console.print("  /code find <pattern>       — Find files by name")
            console.print("  /code search <query>       — Search content in files")
            console.print("  /code read <file>          — Read a code file")
            console.print("  /code deps [path]          — Show dependencies")
            console.print("  /code summary [path]       — Project overview")


def _handle_git_command(args: str, agent):
    """Handle /git subcommands."""
    parts = args.split(maxsplit=1)
    subcmd = parts[0] if parts else "status"
    arg = parts[1] if len(parts) > 1 else ""

    match subcmd:
        case "status" | "st":
            result = asyncio.run(agent.tools.execute("git", {"action": "status"}))
            console.print(result.output)
        case "diff" | "di":
            result = asyncio.run(agent.tools.execute("git", {"action": "diff", "args": arg}))
            console.print(result.output)
        case "commit" | "ci":
            if not arg:
                console.print("[dim]Usage: /git commit <message>[/dim]")
            else:
                result = asyncio.run(agent.tools.execute("git", {"action": "commit", "message": arg}))
                console.print(result.output)
        case "log" | "lg":
            result = asyncio.run(agent.tools.execute("git", {"action": "log"}))
            console.print(result.output)
        case "branch" | "br":
            result = asyncio.run(agent.tools.execute("git", {"action": "branch", "branch": arg}))
            console.print(result.output)
        case _:
            console.print("[dim]Usage:[/dim]")
            console.print("  /git status       — Show working tree status")
            console.print("  /git diff         — Show changes")
            console.print("  /git commit <msg> — Commit changes")
            console.print("  /git log          — Show commit log")
            console.print("  /git branch       — List branches")


def _handle_test_command(args: str, agent):
    """Handle /test command."""
    path = args.strip() if args.strip() else "."
    console.print(f"[yellow]🧪 Running tests in {path}...[/yellow]")
    result = asyncio.run(agent.tools.execute("run_tests", {"path": path}))
    if result.is_success:
        console.print(Panel(result.output, title="✅ Tests Passed", border_style="green"))
    else:
        console.print(Panel(result.output, title="❌ Tests Failed", border_style="red"))


def _handle_schedule_command(args: str, agent):
    """Handle /schedule subcommands."""
    parts = args.split(maxsplit=2)
    subcmd = parts[0] if parts else ""

    match subcmd:
        case "add":
            if len(parts) < 3:
                console.print("[dim]Usage: /schedule add <name> <prompt> --interval 30m[/dim]")
                return
            name = parts[1]
            prompt = parts[2]
            # Parse flags from remaining args
            task = agent.add_scheduled_task(name, prompt, "interval", "1h")
            console.print(f"[success]✅ Scheduled: {name} (every 1h)[/success]")

        case "list" | "ls":
            tasks = agent.list_scheduled_tasks()
            if not tasks:
                console.print("[dim]No scheduled tasks.[/dim]")
                return
            table = Table(title="⏰ Scheduled Tasks", border_style="yellow")
            table.add_column("ID", style="bold")
            table.add_column("Name")
            table.add_column("Schedule")
            table.add_column("Status")
            for t in tasks:
                status = "✅" if t["enabled"] else "⏸️"
                table.add_row(t["id"], t["name"], f"{t['schedule_type']}: {t['schedule_value']}", status)
            console.print(table)

        case "remove" | "rm":
            if len(parts) < 2:
                console.print("[dim]Usage: /schedule remove <task-id>[/dim]")
                return
            if agent.remove_scheduled_task(parts[1]):
                console.print(f"[success]✅ Removed task {parts[1]}[/success]")
            else:
                console.print(f"[error]Task not found: {parts[1]}[/error]")

        case _:
            console.print("[dim]Usage: /schedule <add|list|remove> [args][/dim]")


# ═══════════════════════════════════════════════════
#  MAIN CHAT LOOP
# ═══════════════════════════════════════════════════

def _run_chat(agent, no_stream=False):
    """Main interactive chat loop."""
    info = agent.get_provider_info()

    # Welcome banner
    console.print()
    banner = Text()
    banner.append("  ╔══════════════════════════════════════════════╗\n", style="green")
    banner.append("  ║  🤖 ", style="green")
    banner.append("UTOPIA AGENT", style="bold white")
    banner.append(" — Super-powered Agentic AI  ║\n", style="green")
    banner.append("  ╚══════════════════════════════════════════════╝", style="green")
    console.print(banner)
    console.print()

    # Status line
    console.print(
        f"  🔌 [cyan]{info['provider']}[/cyan] • "
        f"🧠 [cyan]{info['model']}[/cyan] • "
        f"🔧 [yellow]{len(agent.list_tools())} tools[/yellow] • "
        f"💡 [magenta]{len(agent.list_skills())} skills[/magenta] • "
        f"🎭 [green]{agent.config.persona}[/green]"
    )
    console.print(f"  [dim]Type / for commands • /help for help • /quit to exit[/dim]")
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
            console.print(f"\n[error]Configuration Error:[/error]")
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


def _chat_with_animation(agent, user_message: str, no_stream: bool = False):
    """Chat with animated thinking/tool progress."""
    progress = AnimatedProgress()

    # Show thinking animation in background
    def _animate():
        while progress._running:
            frame = progress.get_frame()
            if frame:
                sys.stdout.write(f"\r{frame}   ")
                sys.stdout.flush()
            time.sleep(0.1)
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()

    import threading

    # Start animation
    progress.thinking("🤔 Thinking...")
    anim_thread = threading.Thread(target=_animate, daemon=True)
    anim_thread.start()

    try:
        if no_stream:
            response = asyncio.run(agent.chat(user_message))
            progress.stop()
            time.sleep(0.15)
            console.print()
            console.print(Panel(Markdown(response), title="🤖 Utopia", border_style="green"))
            console.print()
        else:
            # For streaming, show thinking first then stream
            response = asyncio.run(agent.chat(user_message))
            progress.stop()
            time.sleep(0.15)
            console.print()
            console.print(Panel(Markdown(response), title="🤖 Utopia", border_style="green"))
            console.print()
    except Exception:
        progress.stop()
        time.sleep(0.1)
        raise


# ═══════════════════════════════════════════════════
#  CLI ENTRY POINTS
# ═══════════════════════════════════════════════════

@app.command()
def chat(
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="LLM provider"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Model name"),
    api_key: Optional[str] = typer.Option(None, "--api-key", "-k", help="API key"),
    custom_url: Optional[str] = typer.Option(None, "--url", "-u", help="Custom endpoint URL"),
    persona: Optional[str] = typer.Option(None, "--persona", help="Persona name"),
    no_stream: bool = typer.Option(False, "--no-stream", help="Disable streaming"),
):
    """Start interactive chat session."""
    config = _get_config(provider, model, api_key, custom_url)
    if persona:
        config.persona = persona
    agent = _get_agent(config)
    _run_chat(agent, no_stream)


@app.command()
def plan(
    goal: str = typer.Argument(..., help="Goal to plan and execute"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p"),
    model: Optional[str] = typer.Option(None, "--model", "-m"),
):
    """Create a plan and execute it step by step."""
    config = _get_config(provider, model)
    agent = _get_agent(config)
    console.print(f"\n[bold yellow]📋 Planning:[/bold yellow] {goal}\n")
    try:
        plan_result = asyncio.run(agent.plan_and_execute(goal))
        table = Table(title="Execution Plan", border_style="cyan")
        table.add_column("Step", style="bold")
        table.add_column("Thought", max_width=40)
        table.add_column("Action", style="yellow")
        table.add_column("Status")
        for step in plan_result.steps:
            icon = {"completed": "✅", "failed": "❌", "running": "🔄", "pending": "⏳"}.get(step.status, "?")
            table.add_row(str(step.step_number), step.thought[:100], step.action or "-", f"{icon} {step.status}")
        console.print(table)
    except Exception as e:
        console.print(f"[error]Planning failed: {e}[/error]")


@app.command()
def tools():
    """List available tools."""
    agent = _get_agent()
    table = Table(title="🔧 Available Tools", border_style="yellow")
    table.add_column("Tool", style="bold")
    table.add_column("Description", max_width=60)
    for tool in agent.tools.list_tools():
        table.add_row(tool.name, tool.description[:80])
    console.print(table)


@app.command()
def personas():
    """List available personas."""
    agent = _get_agent()
    table = Table(title="🎭 Personas", border_style="magenta")
    table.add_column("Name", style="bold")
    table.add_column("Preview", max_width=60)
    for p in agent.persona_loader.get_all_metadata():
        table.add_row(p["name"], p["preview"])
    console.print(table)


@app.command()
def config_info():
    """Show current configuration."""
    from utopia_agent.config import AgentConfig
    config = AgentConfig()
    llm_config = config.get_llm_config()
    table = Table(title="⚙️ Configuration", border_style="cyan")
    table.add_column("Setting", style="bold")
    table.add_column("Value")
    for key, value in llm_config.items():
        display = value if key != "api_key" else ("***" if value else "(not set)")
        table.add_row(f"llm.{key}", str(display))
    console.print(table)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    provider: Optional[str] = typer.Option(None, "--provider", "-p"),
    model: Optional[str] = typer.Option(None, "--model", "-m"),
    api_key: Optional[str] = typer.Option(None, "--api-key", "-k"),
    custom_url: Optional[str] = typer.Option(None, "--url", "-u"),
):
    """🤖 Utopia Agent — Super-powered Agentic AI with /commands."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(chat, provider=provider, model=model, api_key=api_key, custom_url=custom_url)


if __name__ == "__main__":
    app()
