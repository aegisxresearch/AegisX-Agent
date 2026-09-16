"""Tool-permission commands: /permissions and the tools table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.table import Table

from aegisx_agent.cli.app import console

if TYPE_CHECKING:
    from aegisx_agent.core import AegisXAgent
    from aegisx_agent.tools.base import ToolRisk

#: Colour per risk level, so the gated tools stand out in a listing.
_RISK_STYLES = {"safe": "green", "caution": "yellow", "dangerous": "bold red"}


def _risk_label(risk: ToolRisk) -> str:
    value = risk.value
    return f"[{_RISK_STYLES.get(value, 'white')}]{value}[/]"


def _print_tools_table(
    agent: AegisXAgent,
    max_width: int = 60,
    title: str = "🔧 Available Tools",
    risk_filter: str | None = None,
) -> None:
    """List the agent's tools together with how the gate treats each one.

    ``risk_filter`` (safe|caution|dangerous) limits the listing to one risk
    level; unknown values print an error instead of a table.
    """
    all_tools = agent.tools.list_tools()
    if risk_filter:
        wanted = risk_filter.lower()
        matches = [t for t in all_tools if t.risk.value == wanted]
        if wanted not in {"safe", "caution", "dangerous"}:
            console.print(
                f"[error]Unknown risk '{risk_filter}' — use safe, caution or dangerous[/error]"
            )
            return
        title = f"{title} — {wanted}"
    else:
        matches = all_tools
    table = Table(title=title, border_style="yellow")
    table.add_column("Tool", style="bold")
    table.add_column("Risk")
    table.add_column("Description", max_width=max_width)
    for tool in matches:
        table.add_row(tool.name, _risk_label(tool.risk), tool.description[:80])
    if not matches:
        console.print(f"[warning]No tools with risk '{risk_filter}'.[/warning]")
        return
    console.print(table)
    console.print(
        "[dim]Risk is per call: file_ops 'read' is safe, 'delete' is dangerous. "
        "See /permissions.[/dim]"
    )


def _handle_permissions_command(args: str, agent: AegisXAgent) -> None:
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
