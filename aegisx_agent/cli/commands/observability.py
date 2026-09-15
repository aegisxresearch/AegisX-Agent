"""Observability commands: token-usage summaries and the audit trail viewer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.table import Table

from aegisx_agent.cli.app import console
from aegisx_agent.observability.usage import (
    DELEGATION_FIELD,
    group_delegations,
    parse_natural_time,
    read_usage,
)

if TYPE_CHECKING:
    from aegisx_agent.core import AegisXAgent

USAGE_USAGE = (
    "[dim]Usage:[/dim]\n"
    "  /usage                     — totals for the last 7 days\n"
    "  /usage today|yesterday|7d|24h  — time-filtered totals\n"
    "  /usage --run <id>          — one specific run\n"
    "  /usage --model <name>      — filter by model\n"
    "  /usage --delegations       — only subagent work, one row per delegation"
)

#: How many delegation rows to print before summarizing the tail.
MAX_DELEGATION_ROWS = 10

AUDIT_USAGE = (
    "[dim]Usage:[/dim]\n"
    "  /audit                     — last 20 tool decisions\n"
    "  /audit 50                  — last 50\n"
    "  /audit --denied            — only refusals\n"
    "  /audit --tool shell        — only one tool"
)

_RISK_STYLES = {"safe": "green", "caution": "yellow", "dangerous": "bold red"}


def _format_tokens(count: int) -> str:
    """Compact token count for table cells."""
    return f"{count:,}"


def _usage_entries(
    agent: AegisXAgent,
    period: str | None = None,
    run_id: str | None = None,
    model: str | None = None,
    delegations_only: bool = False,
) -> list[dict[str, Any]]:
    """Load and filter usage entries by period, run, model, and attribution."""
    from aegisx_agent.observability.usage import USAGE_FILE

    path = Path(agent.config.data_path) / USAGE_FILE
    entries = read_usage(path)
    cutoff = parse_natural_time(period) if period else None
    if period and cutoff is None:
        return entries  # unparseable period: leave filtering to the caller
    if cutoff:
        entries = [entry for entry in entries if str(entry.get("timestamp", "")) >= cutoff]
    if run_id:
        entries = [entry for entry in entries if entry.get("run_id") == run_id]
    if model:
        entries = [entry for entry in entries if entry.get("model") == model]
    if delegations_only:
        entries = [entry for entry in entries if entry.get(DELEGATION_FIELD)]
    return entries


def _render_delegations(delegations: list[dict[str, Any]]) -> None:
    """Print one row per delegation — the subagent cost breakdown."""
    table = Table(title="🤖 Per delegation (subagent cost)", border_style="magenta")
    table.add_column("Delegation", style="dim", no_wrap=True)
    table.add_column("Depth", justify="right")
    table.add_column("Task", overflow="ellipsis", max_width=44)
    table.add_column("Calls", justify="right")
    table.add_column("Tokens", justify="right", style="bold")
    for item in delegations[:MAX_DELEGATION_ROWS]:
        table.add_row(
            str(item["delegation"]),
            str(item["depth"]),
            str(item["task"]) or "(unnamed)",
            str(item["calls"]),
            _format_tokens(int(item["total_tokens"])),
        )
    console.print(table)
    hidden = len(delegations) - MAX_DELEGATION_ROWS
    if hidden > 0:
        console.print(
            f"[dim]…and {hidden} more delegation(s); "
            "use --delegations to list only these.[/dim]"
        )


def _print_usage_summary(
    agent: AegisXAgent,
    period: str | None = None,
    run_id: str | None = None,
    model: str | None = None,
    delegations_only: bool = False,
) -> dict[str, Any]:
    """Print a usage summary table and return the aggregate for tests."""
    entries = _usage_entries(agent, period, run_id, model, delegations_only)
    if not entries:
        console.print("[dim]No usage recorded yet for this filter.[/dim]")
        console.print(USAGE_USAGE)
        return {}

    total_tokens = sum(int(entry.get("total_tokens", 0) or 0) for entry in entries)
    input_tokens = sum(int(entry.get("input_tokens", 0) or 0) for entry in entries)
    output_tokens = sum(int(entry.get("output_tokens", 0) or 0) for entry in entries)
    calls = sum(int(entry.get("calls", 0) or 0) for entry in entries)
    runs = len({str(entry.get("run_id", "")) for entry in entries})

    models: dict[str, int] = {}
    for entry in entries:
        model_name = str(entry.get("model", "unknown"))
        models[model_name] = models.get(model_name, 0) + int(entry.get("total_tokens", 0) or 0)

    delegations = group_delegations(entries)
    subagent_tokens = sum(int(item["total_tokens"]) for item in delegations)
    subagent_calls = sum(int(item["calls"]) for item in delegations)
    parent_tokens = total_tokens - subagent_tokens

    title = "📈 Token Usage"
    filters = [
        label
        for label, value in (
            (str(period), period),
            (f"run {run_id}", run_id),
            (f"model {model}", model),
        )
        if value
    ]
    if filters:
        title += f"  ({', '.join(filters)})"

    # A filtered view says so on its own line: the summary table is barely
    # wider than its title, so rich would wrap a longer one mid-word.
    if delegations_only:
        console.print("[dim]Filtered to subagent work only.[/dim]")

    table = Table(title=title, border_style="cyan")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    table.add_row("Runs", str(runs))
    table.add_row("LLM calls", str(calls))
    table.add_row("Input tokens", _format_tokens(input_tokens))
    table.add_row("Output tokens", _format_tokens(output_tokens))
    table.add_row("Total tokens", _format_tokens(total_tokens))
    if delegations:
        # The split is the point of the breakdown: delegation is usually the
        # expensive half, and it is invisible in a single total.
        share = (subagent_tokens / total_tokens * 100) if total_tokens else 0.0
        table.add_row("Parent tokens", _format_tokens(parent_tokens))
        table.add_row("Subagent tokens", _format_tokens(subagent_tokens))
        table.add_row("Subagent calls", str(subagent_calls))
        table.add_row("Subagent share", f"{share:.1f}%")
    console.print(table)

    if delegations:
        _render_delegations(delegations)

    if len(models) > 1:
        model_table = Table(title="Per model", border_style="dim")
        model_table.add_column("Model")
        model_table.add_column("Total tokens", justify="right")
        for model_name, tokens in sorted(models.items(), key=lambda kv: -kv[1]):
            model_table.add_row(model_name, _format_tokens(tokens))
        console.print(model_table)

    latest = max(str(entry.get("timestamp", "")) for entry in entries)
    console.print(f"[dim]usage.jsonl: {Path(agent.config.data_path) / 'usage.jsonl'} "
                  f"(last entry {latest})[/dim]")
    return {
        "runs": runs,
        "calls": calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "parent_tokens": parent_tokens,
        "subagent_calls": subagent_calls,
        "subagent_tokens": subagent_tokens,
        "delegations": delegations,
        "models": models,
        "run_id": run_id,
        "period": period,
        "model": model,
        "delegations_only": delegations_only,
    }


def _handle_usage_command(args: str, agent: AegisXAgent) -> None:
    """Dispatch ``/usage [period] [--run id] [--model name]``."""
    parts = args.split()
    period: str | None = None
    run_id: str | None = None
    model: str | None = None
    delegations_only = False
    index = 0
    while index < len(parts):
        token = parts[index]
        if token in ("--delegations", "--subagents"):
            delegations_only = True
        elif token == "--run" and index + 1 < len(parts):
            run_id = parts[index + 1]
            index += 2
            continue
        elif token == "--model" and index + 1 < len(parts):
            model = parts[index + 1]
            index += 2
            continue
        elif not token.startswith("-"):
            period = token
        index += 1

    if period and parse_natural_time(period) is None and not run_id:
        console.print(
            f"[error]Unknown period '{period}'. Use today|yesterday|<N>d|<N>h.[/error]"
        )
        console.print(USAGE_USAGE)
        return
    _print_usage_summary(agent, period, run_id, model, delegations_only)


def _print_audit_table(
    agent: AegisXAgent,
    limit: int = 20,
    denied_only: bool = False,
    tool: str | None = None,
) -> list[dict[str, Any]]:
    """Print recent tool decisions from the audit log; returns the rows shown."""
    audit = agent.permission_gate.audit
    if not audit.enabled or audit.path is None:
        console.print("[dim]Audit log is disabled "
                      "(AEGISX_AUDIT_LOG_ENABLED=0 or no data dir).[/dim]")
        return []
    entries: list[dict[str, Any]] = [
        entry for entry in audit.tail(limit=1000) if isinstance(entry, dict)
    ]
    if denied_only:
        entries = [entry for entry in entries if entry.get("allowed") is False]
    if tool:
        entries = [entry for entry in entries if entry.get("tool") == tool]

    shown = entries[-limit:]
    if not shown:
        console.print("[dim]No audit entries match.[/dim]")
        return []

    table = Table(title="🛡️ Tool Decision Audit", border_style="red")
    table.add_column("Time", style="dim")
    table.add_column("Tool", style="bold")
    table.add_column("Risk")
    table.add_column("Decision")
    table.add_column("Decided by")
    for entry in shown:
        allowed = entry.get("allowed")
        decision = (
            "[green]allowed[/]" if allowed else "[bold red]denied[/]"
        ) if allowed is not None else str(entry.get("event", "?"))
        risk_value = str(entry.get("risk", ""))
        risk_style = _RISK_STYLES.get(risk_value, "white")
        console_time = str(entry.get("timestamp", ""))[:19]
        table.add_row(
            console_time,
            str(entry.get("tool", "?")),
            f"[{risk_style}]{risk_value}[/]",
            decision,
            str(entry.get("decided_by", "")),
        )
    console.print(table)
    console.print(f"[dim]{audit.path} — every gate decision lands here, "
                  "arguments redacted.[/dim]")
    return shown


def _handle_audit_command(args: str, agent: AegisXAgent) -> None:
    """Dispatch ``/audit [limit] [--denied] [--tool name]``."""
    parts = args.split()
    limit = 20
    denied_only = False
    tool: str | None = None
    index = 0
    while index < len(parts):
        token = parts[index]
        if token == "--denied":
            denied_only = True
        elif token == "--tool" and index + 1 < len(parts):
            tool = parts[index + 1]
            index += 1
        elif token.isdigit():
            limit = max(1, min(int(token), 200))
        index += 1
    _print_audit_table(agent, limit=limit, denied_only=denied_only, tool=tool)


def _usage_to_json(summary: dict[str, Any]) -> str:
    """JSON rendering used by ``aegisx usage --json``."""
    return json.dumps(summary, indent=2, sort_keys=True)


__all__ = [
    "AUDIT_USAGE",
    "USAGE_USAGE",
    "_handle_audit_command",
    "_handle_usage_command",
    "_print_audit_table",
    "_print_usage_summary",
    "_usage_to_json",
]
