"""AegisX Agent CLI — Interactive chat with /commands and animated progress."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import time
from collections.abc import Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import typer
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from aegisx_agent.cli.app import app as app  # re-exported  # noqa: F401
from aegisx_agent.cli.app import console, print_delegation_progress
from aegisx_agent.cli.commands.code import (  # noqa: F401 — re-exported for tests
    _handle_code_command,
    _handle_git_command,
    _handle_test_command,
)
from aegisx_agent.cli.commands.daemon import (  # noqa: F401 — re-exported for tests
    DAEMON_USAGE,
    _handle_daemon_command,
    _print_daemon_status,
    _run_daemon_foreground,
)
from aegisx_agent.cli.commands.mcp import (  # noqa: F401 — re-exported for tests
    MCP_USAGE,
    _handle_mcp_command,
    _load_server_config_file,
    _parse_add_arguments,
    _print_servers_table,
)
from aegisx_agent.cli.commands.observability import (  # noqa: F401 — re-exported for tests
    _handle_audit_command,
    _handle_usage_command,
    _print_audit_table,
    _print_usage_summary,
    _usage_to_json,
)
from aegisx_agent.cli.commands.permissions import (  # noqa: F401 — re-exported
    _handle_permissions_command,
    _print_tools_table,
)
from aegisx_agent.cli.commands.plugins import (  # noqa: F401 — re-exported for tests
    _handle_plugin_command,
    _print_plugins_table,
)
from aegisx_agent.cli.commands.schedule import (  # noqa: F401 — re-exported for tests
    _flags_to_schedule,
    _handle_schedule_command,
    _print_schedule_logs,
    _print_schedule_results,
    _resolve_schedule,
    _split_flags,
)
from aegisx_agent.cli.interactive import (  # noqa: F401 — re-exported for tests
    AnimatedProgress,
    _chat_with_animation,
    _handle_slash_command,
    _print_workspace,
    _read_piped_prompt,
    _run_chat,
    _show_command_menu,
)
from aegisx_agent.mcp.client import MCPClientError
from aegisx_agent.mcp.manager import MCPManagerError

# Global agent and its config path live here so tests can monkeypatch them.
_agent: AegisXAgent | None = None

if TYPE_CHECKING:
    from aegisx_agent.core import AegisXAgent
    from aegisx_agent.core.config import AgentConfig
    from aegisx_agent.security.permissions import PermissionRequest

CONFIG_FILE = Path.home() / ".aegisx" / "config.json"


def _load_saved_config() -> dict[str, Any] | None:
    """Load saved config from ~/.aegisx/config.json."""
    if CONFIG_FILE.exists():
        try:
            import json
            loaded: dict[str, Any] = json.loads(CONFIG_FILE.read_text())
            return loaded
        except (json.JSONDecodeError, Exception):
            pass
    return None


def _save_config(config: AgentConfig) -> None:
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


def _get_config(
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    custom_url: str | None = None,
) -> AgentConfig:
    from aegisx_agent.core.config import AgentConfig, LLMProvider, missing_credentials
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
        try:
            config.llm_provider = LLMProvider(provider.lower())
        except ValueError as exc:
            options = ", ".join(option.value for option in LLMProvider)
            raise typer.BadParameter(
                f"unknown provider '{provider}'. Choose one of: {options}"
            ) from exc
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


def _apply_permission_mode(config: AgentConfig, mode: str | None) -> None:
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


def _get_agent(config: AgentConfig | None = None) -> AegisXAgent:
    global _agent
    if _agent is None:
        from aegisx_agent.core import AegisXAgent
        from aegisx_agent.core.config import AgentConfig

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


def _run_setup_wizard(config: AgentConfig) -> AgentConfig:
    """Interactive setup wizard for first-time users."""
    from aegisx_agent.core.config import LLMProvider

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


#: file-writing tool calls whose arguments carry a before/after preview.
_PREVIEWABLE_TOOLS = {"editor", "file_ops"}
_PREVIEW_MAX_LINES = 40


def _permission_preview(request: PermissionRequest) -> Any | None:
    """A unified diff of the write about to happen, for the approval panel.

    Returns ``None`` when the call is not a file write (nothing to preview)
    or the target file does not exist yet (a create shows its content
    instead, handled by the caller).
    """
    import difflib

    if request.tool not in _PREVIEWABLE_TOOLS:
        return None
    args = request.arguments
    path_arg = args.get("path") or args.get("file")
    content = args.get("content")
    if not path_arg or not isinstance(content, str):
        return None
    path = Path(str(path_arg)).expanduser()
    if args.get("action") not in ("write", "create", "append", "replace_line", "edit"):
        return None
    try:
        old = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    except OSError:
        return None
    if not path.exists() and args.get("action") != "append":
        # New file: show the incoming content itself, truncated.
        lines = content.splitlines()[:_PREVIEW_MAX_LINES]
        more = len(content.splitlines()) - len(lines)
        body = "\n".join(f"+ {line}" for line in lines)
        if more > 0:
            body += f"\n… +{more} more lines"
        return body
    diff = list(difflib.unified_diff(
        old.splitlines(), content.splitlines(),
        fromfile="before", tofile="after", lineterm="",
    ))[2:]  # drop the ---/+++ headers; the panel already says what file
    if not diff:
        return None
    if len(diff) > _PREVIEW_MAX_LINES:
        diff = diff[:_PREVIEW_MAX_LINES] + [f"… +{len(diff) - _PREVIEW_MAX_LINES} more lines"]
    return "\n".join(diff)


async def _permission_prompt(request: PermissionRequest) -> bool:
    """Ask the operator to approve one dangerous tool call.

    Runs inside the agent loop's event loop; ``Prompt.ask`` blocks the loop,
    which is exactly what an interactive CLI wants (the agent must not keep
    working while the question is open).
    """
    console.print()
    console.bell()  # one ping: an approval may be waiting even unfocused
    preview = _permission_preview(request)
    if preview:
        from rich.console import Group
        from rich.syntax import Syntax

        body: Any = Group(
            f"[bold]{request.tool}[/bold] [warning]({request.risk.value})[/bold] "
            f"[cyan]{request.arguments.get('path', '')}[/cyan]\n\n"
            f"{request.summary}\n",
            Syntax(preview, "diff", theme="monokai", word_wrap=True),
        )
    else:
        body = (
            f"[bold]{request.tool}[/bold] [warning]({request.risk.value})[/warning]\n\n"
            f"{request.summary}"
        )
    console.print(Panel(body, title="🔐 Approval required", border_style="yellow"))
    scope_default: str | None = None
    if _agent is not None:
        scope_default = _agent.permission_gate.default_scope_for(request.tool, request.arguments)
    has_scope = scope_default is not None
    choices = ["y", "n", "a"] + (["s"] if has_scope else [])
    scope_hint = ""
    if has_scope:
        scope_hint = f"   [dim]s[/dim] scope: [cyan]{scope_default}[/cyan]"
    answer = Prompt.ask(
        "  [dim]y[/dim] allow once   [dim]n[/dim] deny   [dim]a[/dim] always allow "
        "[dim]'" + request.tool + "'[/dim]" + scope_hint + "\n  Allow?",
        choices=choices,
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
    if answer == "s" and has_scope:
        if _agent is not None and scope_default is not None:
            _agent.permission_gate.allow_scoped(request.tool, scope_default)
        console.print(
            f"[success]✅ '{request.tool}' approved while it stays under "
            f"[cyan]{scope_default}[/cyan][/success]"
        )
        return True
    return answer == "y"


# ═══════════════════════════════════════════════════
#  SCHEDULE TYPER COMMANDS (aegisx schedule ...)
# ═══════════════════════════════════════════════════

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


@schedule_app.command("cancel")
def schedule_cancel(task_id: str = typer.Argument(..., help="Task id")) -> None:
    """Cancel a task now; an active run is interrupted and the task is paused."""
    agent = _get_agent(_get_config())
    if not agent.request_scheduler_cancel(task_id):
        console.print(f"[error]Task not found: {task_id}[/error]")
        raise typer.Exit(code=1)
    console.print(
        f"[warning]⏸️  Cancel requested for task {task_id} — paused at next checkpoint[/warning]"
    )


@schedule_app.command("resume")
def schedule_resume(task_id: str = typer.Argument(..., help="Task id")) -> None:
    """Resume a paused task from its persisted checkpoint."""
    agent = _get_agent(_get_config())
    if not agent.resume_scheduled_task(task_id):
        console.print(f"[error]Task not found: {task_id}[/error]")
        raise typer.Exit(code=1)
    checkpoint = agent.get_scheduler_checkpoint(task_id) or {}
    console.print(f"[success]▶️  Resumed task {task_id}[/success]")
    console.print(f"[dim]Checkpoint state: {checkpoint.get('state', 'unknown')}[/dim]")


@schedule_app.command("checkpoint")
def schedule_checkpoint(task_id: str = typer.Argument(..., help="Task id")) -> None:
    """Show the latest persisted checkpoint of a task."""
    agent = _get_agent(_get_config())
    checkpoint = agent.get_scheduler_checkpoint(task_id)
    if checkpoint is None:
        console.print(f"[error]Task not found: {task_id}[/error]")
        raise typer.Exit(code=1)
    table = Table(title=f"📍 Checkpoint {task_id}", border_style="cyan")
    table.add_column("Field", style="bold")
    table.add_column("Value", max_width=70)
    for key, value in checkpoint.items():
        table.add_row(key, str(value)[:120])
    console.print(table)


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
#  PLUGIN TYPER COMMANDS (aegisx plugin ...)
# ═══════════════════════════════════════════════════

plugin_app = typer.Typer(help="🧩 Manage explicitly loaded tool plugins")


@plugin_app.command("list")
def plugin_list() -> None:
    """List loaded plugins with the permission gate's verdict for each."""
    agent = _get_agent(_get_config())
    _print_plugins_table(agent)


@plugin_app.command("load")
def plugin_load(
    source: str = typer.Argument(..., help="Module path (pkg.mod) or path to a .py file"),
) -> None:
    """Load plugin definitions from a module or an explicit Python file."""
    agent = _get_agent(_get_config())
    loader = agent.load_plugin_path if source.endswith(".py") else agent.load_plugin_module
    try:
        names = loader(source)
    except Exception as exc:
        console.print(f"[error]Plugin load failed: {type(exc).__name__}: {exc}[/error]")
        raise typer.Exit(code=1) from exc
    console.print(f"[success]✅ Loaded {len(names)} plugin(s) from {source}[/success]")
    for name in names:
        console.print(f"[dim]  • {name}[/dim]")
    _print_plugins_table(agent)


@plugin_app.command("unload")
def plugin_unload(
    plugin_id: str = typer.Argument(..., help="The plugin_id of a loaded plugin"),
) -> None:
    """Unload a plugin and remove its tool from the registry."""
    agent = _get_agent(_get_config())
    if not agent.unload_plugin(plugin_id):
        console.print(f"[error]No loaded plugin: {plugin_id}[/error]")
        loaded = [m.plugin_id for m in agent.plugin_registry.list_plugins()]
        if loaded:
            console.print(f"[dim]Loaded: {', '.join(loaded)}[/dim]")
        raise typer.Exit(code=1)
    console.print(f"[success]✅ Unloaded plugin '{plugin_id}'[/success]")


app.add_typer(plugin_app, name="plugin")


# ═══════════════════════════════════════════════════
#  MCP TYPER COMMANDS (aegisx mcp ...)
# ═══════════════════════════════════════════════════

mcp_app = typer.Typer(help="🌐 Manage MCP (Model Context Protocol) servers")


_T = TypeVar("_T")


def _run_async(coro: Coroutine[Any, Any, _T]) -> _T:
    """Run a coroutine to completion from sync Typer command context."""
    return asyncio.run(coro)


@mcp_app.command("list")
def mcp_list() -> None:
    """List configured MCP servers and their connection state."""
    agent = _get_agent(_get_config())
    _print_servers_table(agent)


@mcp_app.command("connect")
def mcp_connect(
    server_id: str = typer.Argument(..., help="Server id from the MCP config"),
    config_file: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="One-shot JSON config (mcpServers document or one server object)",
    ),
) -> None:
    """Connect to an MCP server and register its tools as gated plugins."""
    agent = _get_agent(_get_config())
    try:
        server_config = (
            _load_server_config_file(str(config_file.expanduser()), server_id)
            if config_file
            else None
        )
        names = _run_async(agent.connect_mcp_server(server_id, server_config))
    except (MCPManagerError, MCPClientError, ValueError, OSError, json.JSONDecodeError) as exc:
        console.print(f"[error]MCP connect failed: {type(exc).__name__}: {exc}[/error]")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[success]🌐 Connected to '{server_id}' — {len(names)} tool(s) registered[/success]"
    )
    for name in names:
        tool = agent.tools.get(name)
        risk = tool.risk.value if tool else "?"
        console.print(f"  • {name} [yellow]({risk})[/yellow]")


@mcp_app.command("disconnect")
def mcp_disconnect(
    server_id: str = typer.Argument(..., help="Server id to disconnect"),
) -> None:
    """Disconnect an MCP server and remove its tools from the registry."""
    agent = _get_agent(_get_config())
    if not _run_async(agent.disconnect_mcp_server(server_id)):
        console.print(f"[error]MCP server '{server_id}' is not connected[/error]")
        raise typer.Exit(code=1)
    console.print(f"[success]🔌 Disconnected '{server_id}' — its tools were removed[/success]")


@mcp_app.command("add")
def mcp_add(
    server_id: str = typer.Argument(None, help="Short id used in tool names (mcp_<id>_<tool>)"),
    command: str = typer.Argument(None, help="Executable that speaks MCP over stdio"),
    args: list[str] = typer.Argument(None, help="Arguments for the command"),
    risk: str | None = typer.Option(None, "--risk", help="Default risk: safe, caution, dangerous"),
    wizard: bool = typer.Option(
        False, "--wizard", "-w", help="Guided add: pick a known server, fill values, test-connect"
    ),
) -> None:
    """Persist a stdio MCP server config, or run the guided wizard.

    With no arguments (or --wizard) this becomes interactive: pick from the
    bundled catalog, fill in the placeholders, and the connection is tested
    immediately.
    """
    from aegisx_agent.cli.commands.mcp import _mcp_wizard

    agent = _get_agent(_get_config())
    if wizard or not server_id:
        _mcp_wizard(agent)
        return
    if not command:
        console.print("[error]Usage: aegisx mcp add <id> <command> [args…] — or --wizard[/error]")
        raise typer.Exit(code=1)
    server_config: dict[str, Any] = {"command": command, "args": list(args or [])}
    if risk is not None:
        server_config["risk"] = risk
    try:
        agent.mcp.persist_server(server_id, server_config)
    except MCPManagerError as exc:
        console.print(f"[error]MCP add failed: {exc}[/error]")
        raise typer.Exit(code=1) from exc
    console.print(f"[success]✅ Saved '{server_id}' to {agent.mcp.config_path}[/success]")
    _print_servers_table(agent)


@mcp_app.command("remove")
def mcp_remove(
    server_id: str = typer.Argument(..., help="Server id to remove from the config"),
) -> None:
    """Remove an MCP server from the persisted config."""
    agent = _get_agent(_get_config())
    if not agent.mcp.remove_server_config(server_id):
        console.print(f"[error]No configured MCP server: {server_id}[/error]")
        raise typer.Exit(code=1)
    console.print(f"[success]🗑 Removed '{server_id}' from the MCP config[/success]")


@mcp_app.command("search")
def mcp_search(
    query: str = typer.Argument("", help="Filter by name or description"),
    add: bool = typer.Option(
        False, "--add", "-a", help="Run the wizard for the single matching server"
    ),
) -> None:
    """Search the bundled catalog of well-known MCP servers."""
    from aegisx_agent.mcp.catalog import add_command_hint, search_catalog

    matches = search_catalog(query)
    if not matches:
        console.print(f"[error]No catalog server matches '{query}'[/error]")
        raise typer.Exit(code=1)
    if add:
        if len(matches) > 1:
            ids = ", ".join(sorted(matches))
            console.print(
                f"[error]--add needs exactly one match, got {len(matches)}: {ids}[/error]"
            )
            console.print("[dim]Narrow the query, e.g. aegisx mcp search github --add[/dim]")
            raise typer.Exit(code=1)
        from aegisx_agent.cli.commands.mcp import _mcp_wizard

        _mcp_wizard(_get_agent(_get_config()), preset=next(iter(matches)))
        return
    table = Table(title="🌐 MCP Server Catalog", border_style="cyan")
    table.add_column("ID", style="bold")
    table.add_column("Description", max_width=46)
    table.add_column("Install hint", max_width=60)
    for server_id, entry in matches.items():
        table.add_row(server_id, str(entry.get("description", "")), add_command_hint(server_id))
    console.print(table)
    console.print(
        "[dim]Add one: aegisx mcp search <term> --add, or copy: "
        + add_command_hint(next(iter(matches)))
        + "[/dim]"
    )


@mcp_app.command("doctor")
def mcp_doctor(
    server_id: str = typer.Argument(..., help="Server id to diagnose")
) -> None:
    """Diagnose why an MCP server will not connect."""
    import shutil as _shutil

    agent = _get_agent(_get_config())
    checks: list[tuple[str, str, str]] = []  # (status, check, detail)

    try:
        config = agent.mcp.get_server_config(server_id)
        checks.append(("✅", "config", f"found in {agent.mcp.config_path}"))
    except MCPManagerError as exc:
        checks.append(("❌", "config", str(exc)))
        _print_doctor_report(server_id, checks)
        raise typer.Exit(code=1) from exc

    command = str(config.get("command", ""))
    resolved = _shutil.which(command)
    if resolved:
        checks.append(("✅", "binary", f"{command} → {resolved}"))
    else:
        checks.append(("❌", "binary", f"'{command}' not on PATH — install it first"))

    if agent.mcp.is_connected(server_id):
        names = [d.manifest.qualified_tool_name for d in agent.mcp.definitions_for(server_id)]
        detail = f"live, {len(names)} tool(s): {', '.join(names) or '-'}"
        checks.append(("✅", "connection", detail))
    else:
        checks.append(("➖", "connection", "not connected right now"))

    _print_doctor_report(server_id, checks)

    if not resolved or not agent.mcp.is_connected(server_id):
        console.print(
            "[dim]Try: aegisx mcp connect " + server_id + " — the connect error names the "
            "handshake failure exactly (bad args, missing env, protocol mismatch).[/dim]"
        )


def _print_doctor_report(server_id: str, checks: list[tuple[str, str, str]]) -> None:
    table = Table(title=f"🩺 MCP doctor: {server_id}", border_style="cyan")
    table.add_column("", width=2)
    table.add_column("Check", style="bold")
    table.add_column("Detail", max_width=70)
    for icon, check, detail in checks:
        table.add_row(icon, check, detail)
    console.print(table)


app.add_typer(mcp_app, name="mcp")


# ═══════════════════════════════════════════════════
#  DAEMON TYPER COMMANDS (aegisx daemon run|status)
# ═══════════════════════════════════════════════════

from aegisx_agent.cli.commands.daemon import register as _register_daemon_app  # noqa: E402

_register_daemon_app(app)


# ═══════════════════════════════════════════════════
#  OBSERVABILITY TYPER COMMANDS (aegisx usage / aegisx audit)
# ═══════════════════════════════════════════════════


@app.command("usage")
def usage_command(
    period: str = typer.Argument("7d", help="today, yesterday, <N>d or <N>h"),
    run_id: str | None = typer.Option(None, "--run", help="Only one run id"),
    model: str | None = typer.Option(None, "--model", help="Only one model"),
    delegations: bool = typer.Option(
        False, "--delegations", help="Only subagent work, one row per delegation"
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of a table"),
) -> None:
    """Show token usage recorded by the agent (from usage.jsonl)."""
    agent = _get_agent(_get_config())
    summary = _print_usage_summary(agent, period, run_id, model, delegations)
    if as_json:
        console.print_json(_usage_to_json(summary))


@app.command("audit")
def audit_command(
    limit: int = typer.Argument(20, help="How many recent decisions to show"),
    denied_only: bool = typer.Option(False, "--denied", help="Only refusals"),
    tool: str | None = typer.Option(None, "--tool", help="Only one tool name"),
) -> None:
    """Show the tool-decision audit trail (permission gate log)."""
    agent = _get_agent(_get_config())
    _print_audit_table(agent, limit=max(1, min(limit, 200)), denied_only=denied_only, tool=tool)


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
) -> None:
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
) -> None:
    """Create a plan and execute it step by step."""
    config = _get_config(provider, model)
    _apply_permission_mode(config, permission_mode)
    agent = _get_agent(config)
    console.print(f"\n[bold yellow]📋 Planning:[/bold yellow] {goal}\n")

    def _confirm(p: Any) -> bool:
        table = Table(title="Proposed Plan", border_style="yellow")
        table.add_column("#", style="bold")
        table.add_column("Step", max_width=70)
        table.add_column("Tool", style="yellow")
        for step in p.steps:
            table.add_row(str(step.step_number), step.thought[:120], step.action or "-")
        console.print(table)
        answer = Prompt.ask("Execute this plan?", choices=["y", "n"], default="n")
        return answer == "y"

    try:
        plan_result = asyncio.run(agent.plan_and_execute(goal, confirm=_confirm))
        if getattr(plan_result, "status", "") == "cancelled":
            console.print("[warning]Plan cancelled — nothing was executed.[/warning]")
            return
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
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON (no banners, no progress)"
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
    if not json_output:
        _print_workspace(agent)

    started = time.perf_counter()
    try:
        # A delegated task reports what its subagents are doing as they do it;
        # without this the only feedback until the answer is silence.
        answer = asyncio.run(
            agent.chat(
                text,
                on_progress=None if json_output else print_delegation_progress,
            )
        )
    except Exception as exc:
        if json_output:
            print(json.dumps({"ok": False, "error": str(exc)}))
            raise typer.Exit(code=1) from exc
        console.print(f"[error]Task failed: {exc}[/error]")
        raise typer.Exit(code=1) from exc
    elapsed = time.perf_counter() - started

    if json_output:
        trace = getattr(agent, "last_trace", None)
        print(
            json.dumps(
                {
                    "ok": True,
                    "answer": answer,
                    "session_id": getattr(agent, "session_id", ""),
                    "elapsed_seconds": round(elapsed, 2),
                    "tokens": getattr(trace, "total_tokens", None),
                    "tool_calls": getattr(trace, "total_tool_calls", None),
                }
            )
        )
        return

    console.print()
    console.print(Markdown(answer))
    console.print(f"[dim]done in {elapsed:.1f}s[/dim]")


@app.command()
def tools(
    risk: str = typer.Option(
        "", "--risk", "-r", help="Only show one risk level: safe, caution, dangerous"
    ),
) -> None:
    """List available tools and their risk level."""
    _print_tools_table(_get_agent(), risk_filter=risk or None)


@app.command()
def ingest(
    path: Path = typer.Argument(
        ..., exists=True, readable=True, help="File or directory to ingest"
    ),
) -> None:
    """Ingest documents into the RAG knowledge base."""
    from aegisx_agent.rag.engine import RAGEngine

    agent = _get_agent()
    engine = RAGEngine(
        persist_dir=agent.config.vector_store_path,
        chunk_size=agent.config.rag_chunk_size,
        chunk_overlap=agent.config.rag_chunk_overlap,
    )
    try:
        if path.is_dir():
            chunks = asyncio.run(engine.ingest_directory(str(path)))
        else:
            chunks = asyncio.run(engine.ingest_file(str(path)))
    except ImportError as exc:
        console.print(f"[error]{exc}[/error]")
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        console.print(f"[error]Ingest failed: {exc}[/error]")
        raise typer.Exit(code=1) from exc
    console.print(f"📚 Ingested [cyan]{path}[/cyan] → {chunks} chunks stored")


@app.command("search")
def rag_search(
    query: str = typer.Argument(..., help="What to look for in the knowledge base"),
    top_k: int = typer.Option(3, "--top-k", "-k", min=1, help="Number of results"),
) -> None:
    """Search the RAG knowledge base."""
    from aegisx_agent.rag.engine import RAGEngine

    agent = _get_agent()
    engine = RAGEngine(
        persist_dir=agent.config.vector_store_path,
        chunk_size=agent.config.rag_chunk_size,
        chunk_overlap=agent.config.rag_chunk_overlap,
    )
    try:
        results = asyncio.run(engine.search(query, top_k=top_k))
    except ImportError as exc:
        console.print(f"[error]{exc}[/error]")
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        console.print(f"[error]Search failed: {exc}[/error]")
        raise typer.Exit(code=1) from exc

    if not results:
        console.print("[dim]Knowledge base is empty. Ingest something first:[/dim]")
        console.print("[dim]  aegisx ingest ./docs/[/dim]")
        return

    table = Table(title=f"🔍 {query}", border_style="cyan")
    table.add_column("Score", style="bold", width=6)
    table.add_column("Source", style="yellow", max_width=30)
    table.add_column("Chunk", max_width=70)
    for result in results:
        table.add_row(
            f"{result['score']:.2f}",
            str(result["source"]),
            result["content"].replace("\n", " ")[:200],
        )
    console.print(table)


@app.command()
def personas() -> None:
    """List available personas."""
    agent = _get_agent()
    table = Table(title="🎭 Personas", border_style="magenta")
    table.add_column("Name", style="bold")
    table.add_column("Preview", max_width=60)
    for p in agent.persona_loader.get_all_metadata():
        table.add_row(p["name"], p["preview"])
    console.print(table)


@app.command()
def config_info() -> None:
    """Show current configuration."""
    # _get_config() merges the saved ~/.aegisx/config.json and env — a bare
    # AgentConfig() ignores them, which made this show defaults instead of
    # what `aegisx init` wrote.
    config = _get_config()
    llm_config = config.get_llm_config()
    table = Table(title="⚙️ Configuration", border_style="cyan")
    table.add_column("Setting", style="bold")
    table.add_column("Value")
    for key, value in llm_config.items():
        display = value if key != "api_key" else ("***" if value else "(not set)")
        table.add_row(f"llm.{key}", str(display))
    console.print(table)


@app.command()
def init() -> None:
    """Interactive onboarding: pick provider, model, credentials, AGENTS.md."""
    from aegisx_agent.core.config import LLMProvider, missing_credentials

    console.print("[bold cyan]🚀 AegisX setup wizard[/bold cyan]")
    saved = _load_saved_config() or {}

    # 1. Detect a running local Ollama so the default just works.
    ollama_url = "http://localhost:11434"
    has_ollama = False
    try:
        import urllib.request

        with urllib.request.urlopen(f"{ollama_url}/api/tags", timeout=2) as resp:
            has_ollama = resp.status == 200
    except Exception:  # noqa: BLE001 — any failure just means "not running"
        has_ollama = False
    if has_ollama:
        console.print(f"[success]🔌 Local Ollama detected at {ollama_url}[/success]")

    providers = [p.value for p in LLMProvider]
    default_provider = "ollama" if has_ollama else "openai"
    provider = typer.prompt(
        f"LLM provider ({'/'.join(providers)})",
        default=str(saved.get("provider") or default_provider),
    )
    while provider not in providers:
        provider = typer.prompt(
            f"Unknown provider. Choose one of ({'/'.join(providers)})",
            default=default_provider,
        )

    default_models = {
        "openai": "gpt-4o-mini",
        "anthropic": "claude-sonnet-4",
        "ollama": "llama3.2",
        "groq": "llama-3.3-70b-versatile",
        "custom": "my-model",
    }
    model = typer.prompt(
        "Model",
        default=str(saved.get("model") or default_models.get(provider, "my-model")),
    )

    api_key = ""
    if provider in ("openai", "anthropic", "groq"):
        api_key = typer.prompt(f"{provider} API key", hide_input=True, default="")
    elif provider == "custom":
        api_key = typer.prompt("API key (blank if none)", hide_input=True, default="")

    base_url = ""
    if provider == "ollama":
        base_url = typer.prompt(
            "Ollama base URL", default=str(saved.get("base_url") or ollama_url)
        )
    elif provider == "custom":
        base_url = typer.prompt("Base URL", default=str(saved.get("base_url") or ""))

    config = _get_config(
        provider=provider,
        model=model,
        api_key=api_key or None,
        custom_url=base_url or None,
    )
    missing = missing_credentials(config)
    if missing:
        console.print(
            f"[warning]⚠️ No credentials for '{missing}' yet — you can set the "
            "env var later or re-run `aegisx init`.[/warning]"
        )
    else:
        console.print("[success]✅ Configuration looks complete.[/success]")

    _save_config(config)
    console.print(f"[success]💾 Saved to {CONFIG_FILE}[/success]")

    agents_md = Path.cwd() / "AGENTS.md"
    if not agents_md.exists():
        agents_md.write_text(
            "# AGENTS.md\n\n"
            "Project instructions for AegisX. The agent reads this file from the\n"
            "folder it is launched in.\n\n"
            "## Conventions\n\n"
            "- Describe your project conventions here (language, formatter, tests).\n"
            "- Commands the agent should prefer, e.g. `pytest -q` or `npm test`.\n"
            "- Files or folders it must never touch.\n"
        )
        console.print("[success]📝 Created AGENTS.md starter template.[/success]")

    console.print("[info]Done. Run `aegisx` to start chatting.[/info]")


def _install_root() -> Path:
    """Directory the running aegisx_agent package was loaded from."""
    import aegisx_agent

    return Path(aegisx_agent.__file__).resolve().parents[1]


@app.command()
def update(
    ref: str = typer.Option("main", "--ref", help="Branch or tag to update to"),
) -> None:
    """Update an installer-based install from GitHub."""
    root = _install_root()
    if not (root / ".git").exists():
        console.print(
            "[error]This aegisx install has no git metadata (no .git directory); "
            "re-run the installer to update it.[/error]"
        )
        raise typer.Exit(code=1)

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode != 0:
            console.print(f"[error]git {args[0]} failed: {result.stderr.strip()}[/error]")
            raise typer.Exit(code=1)
        return result.stdout.strip()

    console.print(f"[info]⬆️  Updating from origin/{ref} …[/info]")
    git("fetch", "--depth", "1", "origin", ref)
    current = git("rev-parse", "HEAD")
    target = git("rev-parse", "FETCH_HEAD")
    if current == target:
        console.print(f"[info]Already up to date ({current[:7]}).[/info]")
        return

    if git("status", "--porcelain"):
        console.print(
            "[error]The install has local changes; discarding them automatically "
            "is unsafe. Re-run the installer instead:[/error]"
        )
        console.print(
            "[dim]  curl -fsSL https://raw.githubusercontent.com/aegisxresearch/"
            "AegisX-Agent/main/installer.sh | bash[/dim]"
        )
        raise typer.Exit(code=1)

    git("reset", "--hard", "FETCH_HEAD")
    console.print(
        f"[info]Code updated: {current[:7]} → {target[:7]}. Refreshing the install…[/info]"
    )

    # uv-created venvs ship without pip; if pip is missing, uv made this venv
    # and is therefore on PATH. Editable install means a code-only update is
    # already live — the reinstall step only matters for dependency changes.
    pip_probe = subprocess.run(
        [sys.executable, "-m", "pip", "--version"], capture_output=True, timeout=60
    )
    reinstall: list[list[str]] | None = None
    if pip_probe.returncode == 0:
        reinstall = [[sys.executable, "-m", "pip", "install", "--quiet", "-e", str(root)]]
    elif shutil.which("uv"):
        reinstall = [
            ["uv", "pip", "install", "--python", sys.executable, "-e", str(root)]
        ]

    if reinstall is None:
        console.print(
            "[warning]Code is updated, but neither pip nor uv is available to "
            "refresh dependencies. Re-run the installer if dependencies changed:[/warning]"
        )
        console.print(
            "[dim]  curl -fsSL https://raw.githubusercontent.com/aegisxresearch/"
            "AegisX-Agent/main/installer.sh | bash[/dim]"
        )
        raise typer.Exit(code=0)

    for cmd in reinstall:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            console.print(
                "[error]Reinstall failed — the code on disk is updated, but "
                "dependencies may be stale.[/error]"
            )
            console.print(f"[dim]{result.stderr.strip()[-400:]}[/dim]")
            raise typer.Exit(code=1)
    console.print(f"✓ Updated to {target[:7]}.")


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
) -> None:
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
