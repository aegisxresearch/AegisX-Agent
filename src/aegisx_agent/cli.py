"""AegisX Agent CLI — Interactive chat with /commands and animated progress."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

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
app = typer.Typer(
    name="aegisx",
    help="🤖 AegisX Agent — Super-powered Agentic AI",
    rich_markup_mode="rich",
)

# Global agent
_agent = None

CONFIG_FILE = Path.home() / ".aegisx" / "config.json"


def _load_saved_config() -> dict | None:
    """Load saved config from ~/.aegisx/config.json."""
    if CONFIG_FILE.exists():
        try:
            import json
            return json.loads(CONFIG_FILE.read_text())
        except (json.JSONDecodeError, Exception):
            pass
    return None


def _save_config(config) -> None:
    """Save config to ~/.aegisx/config.json."""
    import json
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "provider": config.llm_provider.value,
        "model": config.get_llm_config().get("model", ""),
        "api_key": config.get_llm_config().get("api_key", ""),
        "base_url": config.get_llm_config().get("base_url", ""),
    }
    CONFIG_FILE.write_text(json.dumps(data, indent=2))


def _announce_local_provider(base_url: str, model: str) -> None:
    """Say which local model was picked, so the choice is not a mystery."""
    console.print(
        f"[success]🔌 Using the local model already running at {base_url}[/success] "
        f"[dim]({model})[/dim]"
    )
    console.print(
        "[dim]Switch any time with /model or /provider, or set AEGISX_LLM_PROVIDER.[/dim]"
    )


def _get_config(provider=None, model=None, api_key=None, custom_url=None):
    from aegisx_agent.config import AgentConfig, LLMProvider, missing_credentials
    config = AgentConfig()

    # Load saved config first
    saved = _load_saved_config()
    if saved:
        try:
            config.llm_provider = LLMProvider(saved.get("provider", "custom"))
        except ValueError:
            config.llm_provider = LLMProvider.CUSTOM

        p = config.llm_provider.value
        m = saved.get("model", "")
        k = saved.get("api_key", "")
        u = saved.get("base_url", "")

        match p:
            case "openai":
                config.openai_model = m or config.openai_model
                config.openai_api_key = k or config.openai_api_key
            case "anthropic":
                config.anthropic_model = m or config.anthropic_model
                config.anthropic_api_key = k or config.anthropic_api_key
            case "ollama":
                config.ollama_model = m or config.ollama_model
                config.ollama_base_url = u or config.ollama_base_url
            case "groq":
                config.groq_model = m or config.groq_model
                config.groq_api_key = k or config.groq_api_key
            case "custom":
                config.custom_model = m or config.custom_model
                config.custom_api_key = k or config.custom_api_key
                config.custom_base_url = u or config.custom_base_url

    # Override with CLI args
    if provider:
        config.llm_provider = provider
    if model:
        match config.llm_provider.value:
            case "openai":
                config.openai_model = model
            case "anthropic":
                config.anthropic_model = model
            case "ollama":
                config.ollama_model = model
            case "groq":
                config.groq_model = model
            case "custom":
                config.custom_model = model
    if api_key:
        match config.llm_provider.value:
            case "openai":
                config.openai_api_key = api_key
            case "anthropic":
                config.anthropic_api_key = api_key
            case "groq":
                config.groq_api_key = api_key
            case "custom":
                config.custom_api_key = api_key
    if custom_url:
        config.custom_base_url = custom_url

    # Zero-config start: with nothing configured, look for a model server that is
    # already running before showing anyone a setup wizard.
    if missing_credentials(config):
        from aegisx_agent.llm.autodetect import detect_local_provider

        detected = detect_local_provider()
        if detected:
            base_url, detected_model = detected
            config.llm_provider = LLMProvider.OLLAMA
            config.ollama_base_url = base_url
            config.ollama_model = detected_model
            _announce_local_provider(base_url, detected_model)

    return config


def _apply_permission_mode(config, mode: str | None) -> None:
    """Apply a ``--permission-mode`` flag value, rejecting unknown names."""
    if not mode:
        return
    from aegisx_agent.security.permissions import PermissionMode

    try:
        config.permission_mode = PermissionMode(mode)
    except ValueError as exc:
        options = ", ".join(option.value for option in PermissionMode)
        raise typer.BadParameter(
            f"unknown permission mode '{mode}'. Choose one of: {options}"
        ) from exc


def _get_agent(config=None):
    global _agent
    if _agent is None:
        from aegisx_agent.config import AgentConfig
        from aegisx_agent.core import AegisXAgent

        if config is None:
            config = AgentConfig()

        try:
            _agent = AegisXAgent(config, prompter=_permission_prompt)
        except ValueError:
            # Auto setup wizard
            console.print()
            console.print(Panel(
                "[bold white]Welcome to AegisX Agent![/bold white]\n\n"
                "Let's set up your AI provider.",
                title="🚀 First Time Setup", border_style="green",
            ))
            console.print()

            config = _run_setup_wizard(config)
            _save_config(config)  # Save for next time
            _agent = AegisXAgent(config, prompter=_permission_prompt)
    return _agent


def _run_setup_wizard(config):
    """Interactive setup wizard for first-time users."""
    from aegisx_agent.config import LLMProvider

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
    "/skills":    {"desc": "List or load learned skills", "icon": "💡"},
    "/code":      {"desc": "Coding mode — analyze/edit/run tests", "icon": "💻"},
    "/git":       {"desc": "Git operations (status/diff/commit)", "icon": "📦"},
    "/test":      {"desc": "Run project tests", "icon": "🧪"},
    "/schedule":  {"desc": "Manage & run scheduled tasks", "icon": "⏰"},
    "/permissions": {"desc": "Show/change tool permissions", "icon": "🔐"},
    "/sessions":  {"desc": "Session stats & search", "icon": "📊"},
    "/learn":     {"desc": "Teach agent a preference", "icon": "🎓"},
    "/config":    {"desc": "Show current config", "icon": "⚙️"},
    "/clear":     {"desc": "Clear conversation memory", "icon": "🧹"},
    "/ingest":    {"desc": "Ingest document (RAG)", "icon": "📚"},
    "/status":    {"desc": "Show agent status", "icon": "📋"},
    "/quit":      {"desc": "Exit AegisX Agent", "icon": "👋"},
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
                title="🤖 AegisX Status", border_style="green",
            ))
            return True

        case "/quit" | "/exit" | "/q":
            console.print("[dim]Goodbye! 👋[/dim]")
            raise SystemExit(0)

    return False


async def _permission_prompt(request) -> bool:
    """Ask the operator to approve one dangerous tool call.

    Runs inside the agent loop's event loop; ``Prompt.ask`` blocks the loop,
    which is exactly what an interactive CLI wants (the agent must not keep
    working while the question is open).
    """
    console.print()
    console.print(Panel(
        f"[bold]{request.tool}[/bold] [warning]({request.risk.value})[/warning]\n\n"
        f"{request.summary}",
        title="🔐 Approval required",
        border_style="yellow",
    ))
    answer = Prompt.ask(
        "  [dim]y[/dim] allow once   [dim]n[/dim] deny   [dim]a[/dim] always allow "
        "[dim]'" + request.tool + "'[/dim]\n  Allow?",
        choices=["y", "n", "a"],
        default="n",
        show_choices=False,
    )
    if answer == "a":
        if _agent is not None:
            _agent.permission_gate.allow(request.tool)
        console.print(
            f"[success]✅ '{request.tool}' is now allow-listed for this session[/success]"
        )
        return True
    return answer == "y"


#: Colour per risk level, so the gated tools stand out in a listing.
_RISK_STYLES = {"safe": "green", "caution": "yellow", "dangerous": "bold red"}


def _risk_label(risk) -> str:
    value = risk.value
    return f"[{_RISK_STYLES.get(value, 'white')}]{value}[/]"


def _print_tools_table(agent, max_width: int = 60, title: str = "🔧 Available Tools") -> None:
    """List the agent's tools together with how the gate treats each one."""
    table = Table(title=title, border_style="yellow")
    table.add_column("Tool", style="bold")
    table.add_column("Risk")
    table.add_column("Description", max_width=max_width)
    for tool in agent.tools.list_tools():
        table.add_row(tool.name, _risk_label(tool.risk), tool.description[:80])
    console.print(table)
    console.print(
        "[dim]Risk is per call: file_ops 'read' is safe, 'delete' is dangerous. "
        "See /permissions.[/dim]"
    )


def _handle_permissions_command(args: str, agent):
    """Show or change which tools the agent may run without asking."""
    parts = args.split()
    action = parts[0].lower() if parts else ""

    if action == "mode":
        if len(parts) < 2:
            console.print("[dim]Usage: /permissions mode <allow-all|ask|read-only>[/dim]")
            return
        try:
            agent.set_permission_mode(parts[1])
            console.print(f"[success]✅ Permission mode: {parts[1]}[/success]")
        except ValueError:
            console.print(f"[error]Unknown mode: {parts[1]}[/error]")
            console.print("[dim]Modes: allow-all, ask, read-only[/dim]")
        return

    if action in {"allow", "deny", "reset"}:
        if len(parts) < 2:
            console.print(f"[dim]Usage: /permissions {action} <tool-name>[/dim]")
            return
        tool = parts[1]
        known = {t.name for t in agent.tools.list_tools()}
        if tool not in known:
            console.print(f"[error]No such tool: {tool}[/error]")
            console.print(f"[dim]Available: {', '.join(sorted(known))}[/dim]")
            return
        if action == "allow":
            agent.permission_gate.allow(tool)
        elif action == "deny":
            agent.permission_gate.deny(tool)
        else:
            agent.permission_gate.clear(tool)
        console.print(f"[success]✅ {action}: {tool}[/success]")
        return

    info = agent.get_permission_info()
    table = Table(title="🔐 Tool Permissions", border_style="yellow")
    table.add_column("Setting", style="bold")
    table.add_column("Value")
    table.add_row("Mode", info["mode"])
    table.add_row("Runtime", "interactive" if info["interactive"] else "unattended (ask ⇒ deny)")
    table.add_row("Allowed (no prompt)", ", ".join(info["allowed"]) or "—")
    table.add_row("Denied", ", ".join(info["denied"]) or "—")
    if info["audit_errors"]:
        table.add_row("[error]Audit errors[/error]", info["audit_errors"])
    console.print(table)
    # Printed outside the table: a path is meant to be copied, not ellipsized.
    console.print(f"[dim]Audit log: {info['audit_log']}[/dim]")

    risky = [
        f"{t.name} ({t.risk.value})"
        for t in agent.tools.list_tools()
        if t.risk.value != "safe"
    ]
    if risky:
        console.print(f"[dim]Gated tools: {', '.join(risky)}[/dim]")

    recent = agent.permission_gate.audit.tail(5)
    if recent:
        log = Table(title="Recent decisions", border_style="dim")
        log.add_column("Time", style="dim")
        log.add_column("Tool")
        log.add_column("Risk")
        log.add_column("Verdict")
        for entry in recent:
            verdict = "✅ allowed" if entry.get("allowed") else "❌ denied"
            log.add_row(
                str(entry.get("timestamp", ""))[11:],
                str(entry.get("tool", "")),
                str(entry.get("risk", "")),
                f"{verdict} [dim]({entry.get('decided_by', '')})[/dim]",
            )
        console.print(log)

    console.print(
        "[dim]Usage: /permissions | /permissions mode <mode> | "
        "/permissions allow|deny|reset <tool>[/dim]"
    )


def _handle_code_command(args: str, agent):
    """Handle /code subcommands."""
    parts = args.split(maxsplit=1)
    subcmd = parts[0] if parts else ""
    arg = parts[1] if len(parts) > 1 else "."

    match subcmd:
        case "structure" | "tree":
            result = asyncio.run(
                agent.tools.execute("codebase", {"action": "structure", "path": arg})
            )
            console.print(result.output)
        case "find":
            result = asyncio.run(
                agent.tools.execute("codebase", {"action": "find", "path": ".", "query": arg})
            )
            console.print(result.output)
        case "search":
            result = asyncio.run(
                agent.tools.execute("codebase", {"action": "search", "path": ".", "query": arg})
            )
            console.print(result.output)
        case "read":
            result = asyncio.run(agent.tools.execute("codebase", {"action": "read", "path": arg}))
            console.print(result.output)
        case "deps":
            result = asyncio.run(agent.tools.execute("codebase", {"action": "deps", "path": arg}))
            console.print(result.output)
        case "summary":
            result = asyncio.run(
                agent.tools.execute("codebase", {"action": "summary", "path": arg})
            )
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
                result = asyncio.run(
                    agent.tools.execute("git", {"action": "commit", "message": arg})
                )
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


SCHEDULE_FLAGS = ("--interval", "--daily", "--weekly", "--cron", "--persona", "--timeout")

SCHEDULE_USAGE = (
    "[dim]Usage:[/dim]\n"
    "  /schedule add <name> <prompt> --interval 30m   — every 30 minutes\n"
    "  /schedule add <name> <prompt> --daily 09:00    — once a day\n"
    "  /schedule add <name> <prompt> --weekly MON:09:00 — once a week\n"
    "  /schedule add <name> <prompt> --cron '*/15 * * * *' — raw cron\n"
    "  /schedule list                                — show tasks\n"
    "  /schedule run                                 — execute everything due now\n"
    "  /schedule logs <task-id>                      — show run history\n"
    "  /schedule remove <task-id>                    — delete a task"
)


def _split_flags(text: str) -> tuple[str, dict[str, str]]:
    """Split ``--flag value`` pairs out of a raw argument string.

    Flags are removed from the body so a prompt containing them is not corrupted.
    """
    tokens = text.split()
    body: list[str] = []
    flags: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in SCHEDULE_FLAGS:
            if index + 1 >= len(tokens):
                raise ValueError(f"{token} requires a value")
            flags[token] = tokens[index + 1]
            index += 2
            continue
        body.append(token)
        index += 1
    return " ".join(body), flags


def _flags_to_schedule(flags: dict[str, str]) -> tuple[str, str]:
    """Map parsed flags onto a ``(schedule_type, schedule_value)`` pair."""
    for flag, schedule_type in (
        ("--interval", "interval"),
        ("--daily", "daily"),
        ("--weekly", "weekly"),
        ("--cron", "cron"),
    ):
        if flag in flags:
            return schedule_type, flags[flag]
    raise ValueError(
        "no schedule given: use --interval 30m, --daily 09:00, "
        "--weekly MON:09:00, or --cron '*/15 * * * *'"
    )


def _resolve_schedule(
    every: str | None, daily: str | None, weekly: str | None, cron: str | None
) -> tuple[str, str]:
    """Validate the mutually exclusive schedule options of ``aegisx schedule add``."""
    provided = {
        flag: value
        for flag, value in (
            ("--interval", every),
            ("--daily", daily),
            ("--weekly", weekly),
            ("--cron", cron),
        )
        if value
    }
    if not provided:
        raise typer.BadParameter("provide one of --interval, --daily, --weekly, or --cron")
    if len(provided) > 1:
        raise typer.BadParameter("provide only one schedule flag")
    return _flags_to_schedule(provided)


def _print_schedule_results(results: list[dict]) -> None:
    """Render the outcome of a scheduler run."""
    if not results:
        console.print("[dim]No tasks are due.[/dim]")
        return
    table = Table(title="⏰ Executed Tasks", border_style="yellow")
    table.add_column("Task", style="bold")
    table.add_column("Status")
    table.add_column("Result", max_width=60)
    for entry in results:
        icon = "✅" if entry["status"] == "completed" else "❌"
        table.add_row(entry["name"], f"{icon} {entry['status']}", entry["result"][:200])
    console.print(table)


def _print_schedule_logs(agent, task_id: str, limit: int = 10) -> None:
    """Render the run history of one scheduled task."""
    logs = agent.get_scheduled_task_logs(task_id, limit=limit)
    if not logs:
        console.print(f"[dim]No run history for task {task_id}.[/dim]")
        return
    table = Table(title=f"📜 Logs for {task_id}", border_style="cyan")
    table.add_column("Time")
    table.add_column("Status")
    table.add_column("Duration")
    table.add_column("Result", max_width=50)
    for entry in logs:
        table.add_row(
            entry["timestamp"],
            entry["status"],
            f"{entry['duration']:.2f}s",
            entry["result"][:120],
        )
    console.print(table)


def _handle_schedule_command(args: str, agent):
    """Handle /schedule subcommands."""
    parts = args.split(maxsplit=1)
    subcmd = parts[0] if parts else ""
    rest = parts[1] if len(parts) > 1 else ""

    match subcmd:
        case "add":
            try:
                body, flags = _split_flags(rest)
                name, _, prompt = body.partition(" ")
                if not name or not prompt.strip():
                    raise ValueError("usage: /schedule add <name> <prompt> --interval 30m")
                schedule_type, schedule_value = _flags_to_schedule(flags)
                task = agent.add_scheduled_task(
                    name,
                    prompt.strip(),
                    schedule_type,
                    schedule_value,
                    persona=flags.get("--persona", "default"),
                    timeout=int(flags.get("--timeout", 120)),
                )
            except ValueError as exc:
                console.print(f"[error]{exc}[/error]")
                return
            console.print(
                f"[success]✅ Scheduled '{task['name']}' "
                f"({schedule_type}: {schedule_value})[/success]"
            )
            console.print(f"[dim]Next run: {task['next_run'] or 'not scheduled'}[/dim]")

        case "list" | "ls":
            tasks = agent.list_scheduled_tasks()
            if not tasks:
                console.print("[dim]No scheduled tasks.[/dim]")
                return
            table = Table(title="⏰ Scheduled Tasks", border_style="yellow")
            table.add_column("ID", style="bold")
            table.add_column("Name")
            table.add_column("Schedule")
            table.add_column("Next run")
            table.add_column("Last run")
            table.add_column("Enabled")
            for t in tasks:
                table.add_row(
                    t["id"],
                    t["name"],
                    f"{t['schedule_type']}: {t['schedule_value']}",
                    t["next_run"] or "-",
                    t["status"],
                    "✅" if t["enabled"] else "⏸️",
                )
            console.print(table)

        case "run":
            _print_schedule_results(asyncio.run(agent.run_due_scheduled_tasks()))

        case "logs":
            if not rest.strip():
                console.print("[dim]Usage: /schedule logs <task-id>[/dim]")
                return
            _print_schedule_logs(agent, rest.strip())

        case "remove" | "rm":
            if not rest.strip():
                console.print("[dim]Usage: /schedule remove <task-id>[/dim]")
                return
            if agent.remove_scheduled_task(rest.strip()):
                console.print(f"[success]✅ Removed task {rest.strip()}[/success]")
            else:
                console.print(f"[error]Task not found: {rest.strip()}[/error]")

        case _:
            console.print(SCHEDULE_USAGE)


schedule_app = typer.Typer(help="⏰ Manage and run scheduled tasks")


@schedule_app.command("add")
def schedule_add(
    name: str = typer.Argument(..., help="Task name"),
    prompt: str = typer.Argument(..., help="Prompt the agent runs on schedule"),
    every: str | None = typer.Option(
        None, "--interval", "-e", help="Interval such as 30m, 2h, 1d"
    ),
    daily: str | None = typer.Option(None, "--daily", help="Run once a day at HH:MM"),
    weekly: str | None = typer.Option(None, "--weekly", help="Run once a week at DAY:HH:MM"),
    cron: str | None = typer.Option(
        None, "--cron", help="Cron expression, e.g. '*/15 * * * *'"
    ),
    persona: str = typer.Option("default", "--persona", help="Persona used for the run"),
    timeout: int = typer.Option(120, "--timeout", help="Max seconds per run"),
) -> None:
    """Schedule a prompt to run automatically."""
    schedule_type, schedule_value = _resolve_schedule(every, daily, weekly, cron)
    agent = _get_agent(_get_config())
    try:
        task = agent.add_scheduled_task(
            name, prompt, schedule_type, schedule_value, persona=persona, timeout=timeout
        )
    except ValueError as exc:
        console.print(f"[error]{exc}[/error]")
        raise typer.Exit(code=1) from exc
    console.print(f"[success]✅ Scheduled '{task['name']}' (id: {task['id']})[/success]")
    console.print(
        f"[dim]{schedule_type}: {schedule_value} • next run: {task['next_run'] or 'n/a'}[/dim]"
    )


@schedule_app.command("list")
def schedule_list() -> None:
    """List scheduled tasks."""
    agent = _get_agent(_get_config())
    tasks = agent.list_scheduled_tasks()
    if not tasks:
        console.print("[dim]No scheduled tasks.[/dim]")
        return
    table = Table(title="⏰ Scheduled Tasks", border_style="yellow")
    table.add_column("ID", style="bold")
    table.add_column("Name")
    table.add_column("Schedule")
    table.add_column("Next run")
    table.add_column("Runs")
    table.add_column("Last run")
    table.add_column("Enabled")
    for task in tasks:
        table.add_row(
            task["id"],
            task["name"],
            f"{task['schedule_type']}: {task['schedule_value']}",
            task["next_run"] or "-",
            str(task["run_count"]),
            task["status"],
            "✅" if task["enabled"] else "⏸️",
        )
    console.print(table)


@schedule_app.command("remove")
def schedule_remove(task_id: str = typer.Argument(..., help="Task id")) -> None:
    """Delete a scheduled task."""
    agent = _get_agent(_get_config())
    if not agent.remove_scheduled_task(task_id):
        console.print(f"[error]Task not found: {task_id}[/error]")
        raise typer.Exit(code=1)
    console.print(f"[success]✅ Removed task {task_id}[/success]")


@schedule_app.command("logs")
def schedule_logs(
    task_id: str = typer.Argument(..., help="Task id"),
    limit: int = typer.Option(10, "--limit", help="How many entries to show"),
) -> None:
    """Show the run history of a scheduled task."""
    _print_schedule_logs(_get_agent(_get_config()), task_id, limit)


@schedule_app.command("run")
def schedule_run(
    once: bool = typer.Option(False, "--once", help="Run everything due once, then exit"),
    interval: int = typer.Option(60, "--interval", "-i", help="Seconds between due-task checks"),
    permission_mode: str | None = typer.Option(
        None,
        "--permission-mode",
        help=(
            "Tool permission mode for unattended runs: allow-all, ask (default), "
            "read-only. In 'ask' nobody can approve, so dangerous tools are denied"
        ),
    ),
) -> None:
    """Execute due tasks now, or keep scheduling until Ctrl+C."""
    config = _get_config()
    _apply_permission_mode(config, permission_mode)
    agent = _get_agent(config)
    if once:
        _print_schedule_results(asyncio.run(agent.run_due_scheduled_tasks()))
        return

    if agent.permission_gate.mode.value == "ask":
        console.print(
            "[warning]🔐 Unattended run: nobody can approve a prompt, so tools that "
            "execute code, run commands, or delete files are denied.[/warning]"
        )
        console.print(
            "[dim]Allow specific ones with AEGISX_ALLOWED_TOOLS=execute_code,run_tests "
            "or pass --permission-mode allow-all.[/dim]"
        )
    console.print(
        f"[info]⏰ Scheduler running — checking due tasks every {interval}s. "
        "Press Ctrl+C to stop.[/info]"
    )
    try:
        asyncio.run(agent.start_scheduler(check_interval=interval))
    except KeyboardInterrupt:
        console.print("\n[dim]Scheduler stopped.[/dim]")


app.add_typer(schedule_app, name="schedule")


# ═══════════════════════════════════════════════════
#  WORKSPACE
# ═══════════════════════════════════════════════════

def _print_workspace(agent) -> None:
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

def _run_chat(agent, no_stream=False):
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


def _chat_with_animation(agent, user_message: str, no_stream: bool = False):
    """Chat with animated thinking/tool progress + streaming."""
    progress = AnimatedProgress()

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

            async def _stream():
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

@app.command()
def chat(
    provider: str | None = typer.Option(None, "--provider", "-p", help="LLM provider"),
    model: str | None = typer.Option(None, "--model", "-m", help="Model name"),
    api_key: str | None = typer.Option(None, "--api-key", "-k", help="API key"),
    custom_url: str | None = typer.Option(None, "--url", "-u", help="Custom endpoint URL"),
    persona: str | None = typer.Option(None, "--persona", help="Persona name"),
    no_stream: bool = typer.Option(False, "--no-stream", help="Disable streaming"),
    permission_mode: str | None = typer.Option(
        None,
        "--permission-mode",
        help="Tool permission mode: allow-all, ask (default), read-only",
    ),
):
    """Start interactive chat session."""
    config = _get_config(provider, model, api_key, custom_url)
    _apply_permission_mode(config, permission_mode)
    # Fix: persona might be OptionInfo object from typer
    if persona and isinstance(persona, str):
        config.persona = persona
    agent = _get_agent(config)
    _run_chat(agent, no_stream)


@app.command()
def plan(
    goal: str = typer.Argument(..., help="Goal to plan and execute"),
    provider: str | None = typer.Option(None, "--provider", "-p"),
    model: str | None = typer.Option(None, "--model", "-m"),
    permission_mode: str | None = typer.Option(
        None,
        "--permission-mode",
        help="Tool permission mode: allow-all, ask (default), read-only",
    ),
):
    """Create a plan and execute it step by step."""
    config = _get_config(provider, model)
    _apply_permission_mode(config, permission_mode)
    agent = _get_agent(config)
    console.print(f"\n[bold yellow]📋 Planning:[/bold yellow] {goal}\n")
    try:
        plan_result = asyncio.run(agent.plan_and_execute(goal))
        table = Table(title="Execution Plan", border_style="cyan")
        table.add_column("Step", style="bold")
        table.add_column("Thought", max_width=40)
        table.add_column("Action", style="yellow")
        table.add_column("Status")
        status_icons = {"completed": "✅", "failed": "❌", "running": "🔄", "pending": "⏳"}
        for step in plan_result.steps:
            icon = status_icons.get(step.status, "?")
            detail = f"{icon} {step.status}"
            table.add_row(str(step.step_number), step.thought[:100], step.action or "-", detail)
        console.print(table)
    except Exception as e:
        console.print(f"[error]Planning failed: {e}[/error]")


@app.command()
def run(
    task: str | None = typer.Argument(
        None, help="Task to run once, then exit (or pipe it on stdin)"
    ),
    provider: str | None = typer.Option(None, "--provider", "-p", help="LLM provider"),
    model: str | None = typer.Option(None, "--model", "-m", help="Model name"),
    api_key: str | None = typer.Option(None, "--api-key", "-k", help="API key"),
    custom_url: str | None = typer.Option(None, "--url", "-u", help="Custom endpoint URL"),
    persona: str | None = typer.Option(None, "--persona", help="Persona name"),
    permission_mode: str | None = typer.Option(
        None,
        "--permission-mode",
        help="Tool permission mode: allow-all, ask (default), read-only",
    ),
) -> None:
    """Run one task in this project and exit — scriptable."""
    text = (task or "").strip() or _read_piped_prompt()
    if not text:
        console.print("[error]No task given.[/error]")
        console.print(
            '[dim]Usage: aegisx run "your task"   or:  echo "task" | aegisx run[/dim]'
        )
        raise typer.Exit(code=1)

    config = _get_config(provider, model, api_key, custom_url)
    _apply_permission_mode(config, permission_mode)
    if persona and isinstance(persona, str):
        config.persona = persona
    agent = _get_agent(config)
    _print_workspace(agent)

    started = time.perf_counter()
    try:
        answer = asyncio.run(agent.chat(text))
    except Exception as exc:
        console.print(f"[error]Task failed: {exc}[/error]")
        raise typer.Exit(code=1) from exc
    elapsed = time.perf_counter() - started

    console.print()
    console.print(Markdown(answer))
    console.print(f"[dim]done in {elapsed:.1f}s[/dim]")


@app.command()
def tools():
    """List available tools and their risk level."""
    _print_tools_table(_get_agent())


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
    from aegisx_agent.config import AgentConfig
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
    provider: str | None = typer.Option(None, "--provider", "-p"),
    model: str | None = typer.Option(None, "--model", "-m"),
    api_key: str | None = typer.Option(None, "--api-key", "-k"),
    custom_url: str | None = typer.Option(None, "--url", "-u"),
    permission_mode: str | None = typer.Option(
        None,
        "--permission-mode",
        help="Tool permission mode: allow-all, ask (default), read-only",
    ),
):
    """🤖 AegisX Agent — Super-powered Agentic AI with /commands."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(
            chat,
            provider=provider,
            model=model,
            api_key=api_key,
            custom_url=custom_url,
            permission_mode=permission_mode,
        )


if __name__ == "__main__":
    app()
