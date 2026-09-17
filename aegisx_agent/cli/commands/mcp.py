"""MCP server management commands: /mcp and the aegisx mcp sub-app."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.table import Table

from aegisx_agent.cli.app import console
from aegisx_agent.mcp.client import MCPClientError
from aegisx_agent.mcp.manager import MCP_SERVERS_KEY, MCPManagerError

if TYPE_CHECKING:
    from aegisx_agent.core import AegisXAgent

MCP_USAGE = (
    "[dim]Usage:[/dim]\n"
    "  /mcp list                                — configured servers and state\n"
    "  /mcp connect <server-id> [config.json]   — connect and register its tools\n"
    "  /mcp connect --all                       — connect every configured server\n"
    "  /mcp disconnect <server-id>              — remove its tools, close the session\n"
    "  /mcp add <server-id> <command> [args…]   — persist a stdio server config\n"
    "  /mcp wizard                              — guided add: pick, fill, test-connect\n"
    "  /mcp remove <server-id>                  — delete it from the config"
)


def _print_servers_table(agent: AegisXAgent, title: str = "🌐 MCP Servers") -> None:
    """List configured MCP servers with their connection state."""
    servers = agent.list_mcp_servers()
    if not servers:
        console.print(
            "[dim]No MCP servers configured. Add one with "
            "/mcp add <server-id> <command> [args…], or edit "
            "~/.aegisx/mcp_servers.json.[/dim]"
        )
        return
    table = Table(title=title, border_style="cyan")
    table.add_column("Server", style="bold")
    table.add_column("Command")
    table.add_column("State")
    table.add_column("Tools")
    for server_id, server_config in servers.items():
        args = " ".join(str(item) for item in server_config.get("args", []))
        command = " ".join(filter(None, (str(server_config.get("command", "")), args)))
        state = (
            "[green]🟢 connected[/]" if server_config.get("connected") else "[dim]disconnected[/]"
        )
        tool_names = [
            manifest.tool_name
            for manifest in agent.plugin_registry.list_plugins()
            if manifest.plugin_id == f"mcp_{server_id}"
        ]
        tools = ", ".join(tool_names) if tool_names else "-"
        table.add_row(server_id, command, state, tools)
    console.print(table)
    console.print(
        "[dim]MCP tools are external code: default risk is caution and they are "
        "refused in read-only mode. Override with the 'risk' / 'tool_risks' config "
        "keys — the permission gate still checks every call.[/dim]"
    )


def _parse_add_arguments(args: str) -> tuple[str, dict[str, object]]:
    """Split ``<server-id> <command> [args…]`` into an id and a server config."""
    parts = args.split()
    if len(parts) < 2:
        raise MCPManagerError(
            "Expected: /mcp add <server-id> <command> [args…] "
            "(example: /mcp add files npx -y @modelcontextprotocol/server-filesystem /tmp)"
        )
    server_id, command, *rest = parts
    return server_id, {"command": command, "args": rest}


def _load_server_config_file(path: str, server_id: str) -> dict[str, object]:
    """Read one server config from a JSON file.

    Accepts either a full ``{"mcpServers": {...}}`` document (Claude Desktop
    style) or a single server config object.
    """
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if isinstance(data, dict) and MCP_SERVERS_KEY in data:
        servers = data[MCP_SERVERS_KEY]
        if server_id not in servers:
            raise MCPManagerError(
                f"'{MCP_SERVERS_KEY}' in {path} has no entry '{server_id}': "
                f"{sorted(servers)}"
            )
        server_config = servers[server_id]
    else:
        server_config = data
    if not isinstance(server_config, dict):
        raise MCPManagerError(f"Server config in {path} must be a JSON object")
    return server_config


def _connect(agent: AegisXAgent, server_id: str, config_path: str | None) -> None:
    """Connect to a server and print every tool it brought online."""
    try:
        server_config = (
            _load_server_config_file(config_path, server_id) if config_path else None
        )
        names = asyncio.run(agent.connect_mcp_server(server_id, server_config))
    except (MCPManagerError, MCPClientError, ValueError, OSError, json.JSONDecodeError) as exc:
        console.print(f"[error]MCP connect failed: {exc}[/error]")
        return
    console.print(
        f"[success]🌐 Connected to '{server_id}' — {len(names)} tool(s) registered:[/success]"
    )
    for name in names:
        tool = agent.tools.get(name)
        risk = tool.risk.value if tool else "?"
        console.print(f"  • {name} [yellow]({risk})[/yellow]")
    console.print(
        "[dim]Every call still passes the permission gate "
        f"(mode: {agent.permission_gate.mode.value}).[/dim]"
    )


def _connect_all(agent: AegisXAgent) -> bool:
    """Connect every configured server; ``True`` when none failed."""
    try:
        connected, failures = asyncio.run(agent.connect_all_mcp_servers())
    except (MCPManagerError, ValueError) as exc:
        console.print(f"[error]MCP connect --all failed: {exc}[/error]")
        return False
    if not connected and not failures:
        console.print(
            f"[warning]No MCP servers configured in {agent.mcp.config_path}[/warning]"
        )
        console.print("[dim]Add one with `/mcp wizard`.[/dim]")
        return False
    table = Table(title="🌐 MCP connect --all")
    table.add_column("Server", style="bold")
    table.add_column("Result")
    table.add_column("Detail", max_width=60)
    for server_id, names in connected.items():
        table.add_row(server_id, "[success]connected[/success]", f"{len(names)} tool(s)")
    for server_id, error in failures.items():
        table.add_row(server_id, "[error]failed[/error]", error)
    console.print(table)
    for server_id in failures:
        _print_doctor_hint(agent, server_id)
    return not failures


def _disconnect(agent: AegisXAgent, server_id: str) -> None:
    """Disconnect a server, reporting unknown ids instead of raising."""
    if not asyncio.run(agent.disconnect_mcp_server(server_id)):
        console.print(f"[error]MCP server '{server_id}' is not connected[/error]")
        console.print(f"[dim]Connected: {', '.join(agent.mcp.connected_servers()) or '-'}[/dim]")
        return
    console.print(f"[success]🔌 Disconnected '{server_id}' — its tools were removed[/success]")


# ═══════════════════════════════════════════════════
#  INTERACTIVE ADD WIZARD
# ═══════════════════════════════════════════════════

def _mcp_wizard(agent: AegisXAgent, preset: str | None = None) -> None:
    """Guided add: pick a known server (or type your own), fill placeholders,
    save, then connect right away so failure surfaces here — not later.

    ``preset`` skips the catalog menu and jumps straight to a known entry —
    used by ``aegisx mcp search <term> --add``.
    """
    from rich.prompt import Prompt

    from aegisx_agent.mcp.catalog import CATALOG

    entries = sorted(CATALOG)
    if preset is not None and preset not in CATALOG:
        console.print(
            f"[error]'{preset}' is not in the bundled catalog. "
            f"Known: {', '.join(entries)}[/error]"
        )
        return

    console.print("[bold cyan]🧭 MCP server wizard[/bold cyan]")

    # 1. Pick a server: numbered catalog or a free-form entry.
    if preset is not None:
        choice = str(entries.index(preset) + 1)
    else:
        console.print("[info]Well-known servers:[/info]")
        for index, server_id in enumerate(entries, 1):
            desc = CATALOG[server_id]["description"]
            console.print(f"  [cyan]{index}[/cyan]. {server_id} — {desc}")
        console.print("  [cyan]m[/cyan]. manual — type your own command")
        choice = Prompt.ask("Pick a number or 'm'", default="m")

    if choice.strip().isdigit() and 1 <= int(choice.strip()) <= len(entries):
        server_id = entries[int(choice.strip()) - 1]
        template = dict(CATALOG[server_id])
        args_list = [str(item) for item in template.get("args", [])]
        # 2. Fill any <placeholder> tokens interactively.
        filled: list[str] = []
        for arg in args_list:
            if "<" in arg and ">" in arg:
                placeholder = arg[arg.find("<") : arg.find(">") + 1]
                value = Prompt.ask(f"  {placeholder.replace('<', '').replace('>', '')}")
                if not value:
                    console.print("[error]A value is required for this server.[/error]")
                    return
                arg = arg.replace(placeholder, value)
            filled.append(arg)
        server_config: dict[str, Any] = {
            "command": str(template.get("command", "")),
            "args": filled,
        }
        env_hint = template.get("env")
        if env_hint:
            env: dict[str, str] = {}
            for key, sample in env_hint.items():
                entered = Prompt.ask(f"  env {key} (blank to skip)", password=True, default="")
                if entered:
                    env[key] = entered
            if env:
                server_config["env"] = env
    else:
        server_id = Prompt.ask("Server id (short, used in tool names)")
        if not server_id.strip():
            console.print("[error]A server id is required.[/error]")
            return
        command = Prompt.ask("Command (e.g. npx, uvx, python)")
        raw_args = Prompt.ask("Arguments (space-separated, blank if none)", default="")
        server_config = {"command": command.strip(), "args": raw_args.split()}

    # 3. Save it.
    try:
        agent.mcp.persist_server(server_id, server_config)
    except (MCPManagerError, ValueError) as exc:
        console.print(f"[error]MCP add failed: {exc}[/error]")
        return
    console.print(f"[success]✅ Saved '{server_id}' to {agent.mcp.config_path}[/success]")

    # 4. Test the connection immediately — the whole point of the wizard.
    console.print(f"[info]🔎 Testing connection to '{server_id}' …[/info]")
    try:
        names = asyncio.run(agent.connect_mcp_server(server_id, server_config))
    except (MCPClientError, MCPManagerError, OSError) as exc:
        console.print(f"[error]Connection test failed: {type(exc).__name__}: {exc}[/error]")
        console.print(
            "[dim]The config was saved. Fix the problem (install the binary, check "
            "args/env), then run: aegisx mcp connect " + server_id + "[/dim]"
        )
        _print_doctor_hint(agent, server_id)
        return
    console.print(
        f"[success]🌐 Connected — {len(names)} tool(s) registered:[/success]"
    )
    for name in names:
        tool = agent.tools.get(name)
        risk = tool.risk.value if tool else "?"
        console.print(f"  • {name} [yellow]({risk})[/yellow]")


def _print_doctor_hint(agent: AegisXAgent, server_id: str) -> None:
    """Suggest doctor checks after a failed wizard connect."""
    import shutil

    try:
        config = agent.mcp.get_server_config(server_id)
    except MCPManagerError:
        return
    command = str(config.get("command", ""))
    if not shutil.which(command):
        console.print(f"[dim]🩺 '{command}' is not on PATH — install it first.[/dim]")
    console.print(f"[dim]🩺 Details: aegisx mcp doctor {server_id}[/dim]")


def _handle_mcp_command(args: str, agent: AegisXAgent) -> None:
    """Dispatch ``/mcp <subcommand>``."""
    parts = args.strip().split(maxsplit=1)
    subcommand = parts[0].lower() if parts else ""
    rest = parts[1] if len(parts) > 1 else ""

    if subcommand in ("", "help"):
        console.print(MCP_USAGE)
        return
    if subcommand == "list":
        _print_servers_table(agent)
        return
    if subcommand == "connect":
        connect_parts = rest.split(maxsplit=1)
        if connect_parts and connect_parts[0] in ("--all", "-a"):
            _connect_all(agent)
            return
        if not connect_parts:
            console.print(
                "[error]Usage: /mcp connect <server-id> [config.json][/error] "
                "— or /mcp connect --all"
            )
            return
        _connect(agent, connect_parts[0], connect_parts[1] if len(connect_parts) > 1 else None)
        return
    if subcommand == "disconnect":
        if not rest:
            console.print("[error]Usage: /mcp disconnect <server-id>[/error]")
            return
        _disconnect(agent, rest.strip())
        return
    if subcommand == "add":
        if not rest:
            _mcp_wizard(agent)
            return
        try:
            server_id, server_config = _parse_add_arguments(rest)
            agent.mcp.persist_server(server_id, dict(server_config))
        except (MCPManagerError, ValueError) as exc:
            console.print(f"[error]MCP add failed: {exc}[/error]")
            return
        console.print(
            f"[success]✅ Saved '{server_id}' to {agent.mcp.config_path}[/success]"
        )
        _print_servers_table(agent)
        return
    if subcommand in ("wizard", "w"):
        _mcp_wizard(agent)
        return
    if subcommand == "remove":
        if not agent.mcp.remove_server_config(rest.strip()):
            console.print(f"[error]No configured MCP server: {rest.strip()}[/error]")
            return
        console.print(f"[success]🗑 Removed '{rest.strip()}' from the MCP config[/success]")
        return
    console.print(f"[error]Unknown /mcp subcommand: {subcommand}[/error]")
    console.print(MCP_USAGE)


__all__ = [
    "MCP_USAGE",
    "_connect_all",
    "_handle_mcp_command",
    "_print_doctor_hint",
    "_print_servers_table",
]
