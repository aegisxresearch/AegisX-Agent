"""Plugin management commands: /plugin and the aegisx plugin sub-app."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.table import Table

from aegisx_agent.cli.app import console

if TYPE_CHECKING:
    from aegisx_agent.core import AegisXAgent
    from aegisx_agent.tools.base import Tool, ToolRisk

PLUGIN_USAGE = (
    "[dim]Usage:[/dim]\n"
    "  /plugin list                     — loaded plugins and gate verdicts\n"
    "  /plugin load <module-or-path>    — load a module or .py file\n"
    "  /plugin unload <plugin-id>       — remove a loaded plugin"
)

_RISK_STYLES = {"safe": "green", "caution": "yellow", "dangerous": "bold red"}


def _risk_label(risk: ToolRisk) -> str:
    value = risk.value
    return f"[{_RISK_STYLES.get(value, 'white')}]{value}[/]"


def _gate_verdict(agent: AegisXAgent, tool: Tool) -> str:
    """Describe what the permission gate would do to this plugin's calls."""
    mode = agent.permission_gate.mode
    if tool.name in agent.permission_gate.denied_tools:
        return "[bold red]denied (denylist)[/]"
    if tool.name in agent.permission_gate.allowed_tools:
        return "[green]allowed (allowlist)[/]"
    risk = tool.risk
    if mode.value == "read-only":
        return "[green]allowed[/]" if risk.value == "safe" else "[bold red]denied[/]"
    if mode.value == "allow-all":
        return "[green]allowed[/]"
    if risk.value == "dangerous":
        if agent.permission_gate.interactive:
            return "[yellow]asks first[/]"
        return "[bold red]denied (unattended)[/]"
    return "[green]allowed[/]"


def _print_plugins_table(agent: AegisXAgent, title: str = "🧩 Loaded Plugins") -> None:
    """List loaded plugins with their schema, policy, and gate verdict."""
    plugins = agent.plugin_registry.list_plugins()
    if not plugins:
        console.print("[dim]No plugins loaded. Use /plugin load <module-or-path>.[/dim]")
        return
    table = Table(title=title, border_style="magenta")
    table.add_column("Plugin", style="bold")
    table.add_column("Version")
    table.add_column("Tool")
    table.add_column("Risk")
    table.add_column("Gate verdict")
    table.add_column("Description", max_width=46)
    for manifest in plugins:
        tool = agent.tools.get(manifest.qualified_tool_name)
        verdict = _gate_verdict(agent, tool) if tool else "[error]not registered[/]"
        table.add_row(
            manifest.plugin_id,
            manifest.version,
            manifest.qualified_tool_name,
            _risk_label(tool.risk) if tool else "-",
            verdict,
            manifest.description[:80],
        )
    console.print(table)
    console.print(
        f"[dim]Permission mode: {agent.permission_gate.mode.value} — plugins never "
        "bypass the gate; change policy with /permissions.[/dim]"
    )


def _handle_plugin_command(args: str, agent: AegisXAgent) -> None:
    """Handle /plugin subcommands in the chat loop."""
    parts = args.split(maxsplit=1)
    subcmd = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if subcmd == "list":
        _print_plugins_table(agent)
        return

    if subcmd == "load":
        if not rest:
            console.print("[dim]Usage: /plugin load <module-or-path>[/dim]")
            return
        source = rest
        try:
            loader = (
                agent.load_plugin_path if source.endswith(".py") else agent.load_plugin_module
            )
            names = loader(source)
        except Exception as exc:
            console.print(f"[error]Plugin load failed: {type(exc).__name__}: {exc}[/error]")
            return
        console.print(f"[success]✅ Loaded {len(names)} plugin(s) from {source}[/success]")
        for name in names:
            console.print(f"[dim]  • {name}[/dim]")
        _print_plugins_table(agent)
        return

    if subcmd == "unload":
        if not rest:
            console.print("[dim]Usage: /plugin unload <plugin-id>[/dim]")
            return
        if agent.unload_plugin(rest):
            console.print(f"[success]✅ Unloaded plugin '{rest}'[/success]")
        else:
            console.print(f"[error]No loaded plugin: {rest}[/error]")
            loaded = [m.plugin_id for m in agent.plugin_registry.list_plugins()]
            if loaded:
                console.print(f"[dim]Loaded: {', '.join(loaded)}[/dim]")
        return

    console.print(PLUGIN_USAGE)
