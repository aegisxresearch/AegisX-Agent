"""Cyber commands — the CLI face of the AegisX security engine.

Six verbs, each one a different way of asking the same question:

- ``scan``   — the deterministic pipeline: recon, checks, report.
- ``sast``   — static analysis of a source tree.
- ``audit``  — dependency advisory lookup.
- ``swarm``  — several lines of attack at once, without the model.
- ``agent``  — the autonomous loop: survey, plan, execute, verify.
- ``report`` — re-render a previous run's findings into a new format.

The deterministic commands never need a model, so they work on a machine
with no API key. The ``agent`` command does need one; without a
provider it still runs, but it runs the engine's default plan and
skips the review stage, and says so in the output.

Every command comes in two layers: a plain implementation function
(``run_scan``, ``run_sast``, …) that takes ordinary arguments, and a
thin Typer wrapper that only parses the command line. The interactive
``/cyber`` slash command calls the implementation layer directly, so
the two entry points can never drift apart.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer
from rich.table import Table

from aegisx_agent.cli.app import console

#: Colours per severity, used by the findings table.
_SEVERITY_STYLE: dict[str, str] = {
    "critical": "bold red",
    "high": "red",
    "medium": "yellow",
    "low": "cyan",
    "info": "dim",
}


class CyberCommandError(RuntimeError):
    """A cyber command could not complete.

    The implementation layer raises this instead of ``typer.Exit`` so
    the same code can serve both the CLI (which turns it into an exit
    code) and the interactive session (which turns it into a message).
    """


def _print_findings(findings: list[Any], *, limit: int = 40) -> None:
    """Print a findings table, worst first."""
    if not findings:
        console.print("[dim]No findings.[/dim]")
        return

    table = Table(title="Findings", border_style="magenta", show_lines=False)
    table.add_column("Sev", width=9)
    table.add_column("Confidence", width=11)
    table.add_column("Category", width=11)
    table.add_column("Finding")
    table.add_column("Location", max_width=44, overflow="ellipsis")

    for finding in findings[:limit]:
        severity = getattr(finding, "severity", None)
        severity_text = str(getattr(severity, "value", severity))
        confidence = getattr(finding, "confidence", None)
        confidence_text = str(getattr(confidence, "value", confidence))
        category = getattr(finding, "category", None)
        category_text = str(getattr(category, "value", category))
        table.add_row(
            f"[{_SEVERITY_STYLE.get(severity_text, 'white')}]{severity_text.upper()}[/]",
            confidence_text,
            category_text,
            str(getattr(finding, "title", "")),
            str(getattr(finding, "endpoint", "") or "-"),
        )
    console.print(table)
    if len(findings) > limit:
        console.print(f"[dim]… and {len(findings) - limit} more findings.[/dim]")


def _print_histogram(histogram: dict[str, int]) -> None:
    """Print a severity histogram."""
    if not histogram:
        return
    total = sum(histogram.values()) or 1
    parts = [
        f"[{_SEVERITY_STYLE.get(severity, 'white')}]{severity}:{count}[/]"
        for severity, count in sorted(
            histogram.items(),
            key=lambda item: (
                ["critical", "high", "medium", "low", "info"].index(item[0])
                if item[0] in ["critical", "high", "medium", "low", "info"]
                else 99
            ),
        )
    ]
    console.print(f"[dim]{total} finding(s) — {' '.join(parts)}[/dim]")


def _severity_filter(level: str) -> Any:
    """Map a severity name onto the model's severity enum."""
    from aegisx_agent.cyber.models import Severity

    try:
        return Severity(level.lower())
    except ValueError as exc:
        raise CyberCommandError(
            f"Unknown severity {level!r}. Use one of: " + ", ".join(item.value for item in Severity)
        ) from exc


def _formats(report_format: str) -> tuple[str, ...]:
    """Turn the ``--report-format`` value into a tuple of formats."""
    if report_format == "all":
        return ("sarif", "markdown", "html")
    if report_format not in {"sarif", "markdown", "html"}:
        raise CyberCommandError(
            f"Unknown report format {report_format!r}. Use sarif, markdown, html, or all."
        )
    return (report_format,)


# ═══════════════════════════════════════════════════
#  IMPLEMENTATIONS
# ═══════════════════════════════════════════════════


def run_scan(
    target: str,
    *,
    deep: bool = True,
    max_requests: int = 2000,
    rate: float = 10.0,
    concurrency: int = 6,
    timeout: float = 1800.0,
    report_dir: str = "",
    report_format: str = "all",
    minimum_severity: str = "info",
    allow_private: bool = False,
    as_json: bool = False,
) -> None:
    """Run a deterministic security scan against an authorised target."""
    from aegisx_agent.cyber.scanner import ScanConfig, Scanner, summarise

    config = ScanConfig(
        targets=[target],
        deep=deep,
        max_requests=max_requests,
        requests_per_second=rate,
        max_concurrency=concurrency,
        timeout_seconds=timeout,
        allow_private_addresses=allow_private,
    )

    async def _run() -> Any:
        scanner = Scanner(config)
        scanner.on_progress(
            lambda event: console.print(f"[dim]{event.phase}: {event.message}[/dim]")
        )
        return await scanner.run()

    try:
        result = asyncio.run(_run())
    except (ValueError, OSError) as exc:
        raise CyberCommandError(f"{type(exc).__name__}: {exc}") from exc

    if as_json:
        console.print_json(json.dumps(result.to_dict(), default=str))
        return

    console.print(f"[success]{summarise(result)}[/success]")
    _print_histogram(result.stats.findings_by_severity)
    _print_findings(result.sorted_findings())

    if report_dir:
        from aegisx_agent.cyber.report import ReportOptions, write_reports

        options = ReportOptions(minimum_severity=_severity_filter(minimum_severity))
        written = write_reports(
            result, report_dir, options=options, formats=_formats(report_format)
        )
        for name, path in written.items():
            console.print(f"[success]📄 {name}: {path}[/success]")


def run_sast(
    path: str = ".",
    *,
    report_dir: str = "",
    report_format: str = "all",
    minimum_severity: str = "info",
    as_json: bool = False,
) -> None:
    """Statically analyse a source tree for secrets, sinks, and misconfiguration."""
    from aegisx_agent.cyber.models import ScanResult
    from aegisx_agent.cyber.report import ReportOptions, write_reports
    from aegisx_agent.cyber.sast import StaticAnalyzer

    root = Path(path).expanduser()
    if not root.exists():
        raise CyberCommandError(f"Path not found: {root}")

    outcome = StaticAnalyzer().analyze(root)

    if as_json:
        console.print_json(json.dumps(outcome.to_dict(), default=str))
        return

    console.print(
        f"[success]Scanned {outcome.files_scanned} files under {root}: "
        f"{len(outcome.findings)} findings[/success]"
    )
    _print_findings(outcome.findings)

    if report_dir:
        result = ScanResult(target=str(root))
        result.findings = list(outcome.findings)
        result.finalize()
        options = ReportOptions(minimum_severity=_severity_filter(minimum_severity))
        written = write_reports(
            result, report_dir, options=options, formats=_formats(report_format)
        )
        for name, report_path in written.items():
            console.print(f"[success]📄 {name}: {report_path}[/success]")


def run_audit(path: str = ".", *, as_json: bool = False) -> None:
    """Check a project's dependencies against the advisory database."""
    from aegisx_agent.cyber.cve import VulnerabilityAuditor
    from aegisx_agent.cyber.sast import parse_dependencies

    root = Path(path).expanduser()
    if not root.exists():
        raise CyberCommandError(f"Path not found: {root}")

    dependencies = parse_dependencies(root)
    if not dependencies:
        console.print(f"[dim]No dependency manifests were found under {root}.[/dim]")
        return

    async def _run() -> Any:
        return await VulnerabilityAuditor().audit_dependencies(dependencies)

    try:
        report = asyncio.run(_run())
    except (ValueError, OSError) as exc:
        raise CyberCommandError(f"{type(exc).__name__}: {exc}") from exc

    if as_json:
        console.print_json(json.dumps(report.to_dict(), default=str))
        return

    suffix = " (offline knowledge base used)" if report.offline_used else ""
    console.print(
        f"[success]Checked {report.packages_checked} dependencies{suffix}: "
        f"{len(report.findings)} advisories apply[/success]"
    )
    _print_findings(report.findings)
    if report.errors:
        console.print(f"[warning]{len(report.errors)} lookup error(s):[/warning]")
        for error in report.errors[:5]:
            console.print(f"[dim]  {error}[/dim]")


def run_swarm(
    target: str,
    *,
    max_requests: int = 2000,
    concurrency: int = 4,
    timeout: float = 1800.0,
    report_dir: str = "",
    as_json: bool = False,
) -> None:
    """Run several lines of attack concurrently, without the model."""
    from aegisx_agent.cyber.scope import ScopeGuard, scope_from_targets
    from aegisx_agent.cyber.swarm import SwarmPlanner, SwarmRunner

    base_url = target if "://" in target else f"https://{target}"
    try:
        policy = scope_from_targets(
            [target],
            max_requests=max_requests,
            max_concurrency=concurrency,
            timeout_seconds=timeout,
        )
    except ValueError as exc:
        raise CyberCommandError(str(exc)) from exc

    guard = ScopeGuard(policy)
    plan = SwarmPlanner(base_url, max_concurrency=concurrency).plan(target)

    async def _run() -> Any:
        return await SwarmRunner(plan, guard=guard).run()

    try:
        result = asyncio.run(_run())
    except (ValueError, OSError) as exc:
        raise CyberCommandError(f"{type(exc).__name__}: {exc}") from exc

    if as_json:
        console.print_json(json.dumps(result.to_dict(), default=str))
        return

    console.print(
        f"[success]{len(result.lines)} lines of attack → {len(result.findings)} findings "
        f"({len(result.corroborated)} corroborated)[/success]"
    )
    for line in result.lines:
        status = f"[error]{line.error}[/error]" if line.error else f"{len(line.findings)} findings"
        console.print(f"[dim]  • {line.name}: {status}[/dim]")
    _print_findings(result.findings)

    if report_dir:
        from aegisx_agent.cyber.models import ScanResult
        from aegisx_agent.cyber.report import write_reports

        scan_result = ScanResult(target=target, scope=", ".join(policy.allowed_hosts))
        scan_result.findings = list(result.findings)
        scan_result.stats = result.stats
        scan_result.finalize()
        written = write_reports(scan_result, report_dir)
        for name, path in written.items():
            console.print(f"[success]📄 {name}: {path}[/success]")


def run_agent(
    target: str,
    *,
    max_requests: int = 2000,
    concurrency: int = 4,
    timeout: float = 1800.0,
    verify: bool = True,
    report_dir: str = "",
    as_json: bool = False,
) -> None:
    """Run the autonomous assessment: survey, plan, execute, verify, report."""
    from aegisx_agent.cyber.agent import CyberAgent, CyberAgentConfig

    config = CyberAgentConfig(
        targets=[target],
        max_requests=max_requests,
        max_concurrency=concurrency,
        timeout_seconds=timeout,
        skip_verification=not verify,
    )

    model = _cyber_model()

    def _progress(phase: str, message: str) -> None:
        console.print(f"[dim]{phase}: {message}[/dim]")

    async def _run() -> Any:
        return await CyberAgent(config, model=model, on_progress=_progress).run()

    try:
        run = asyncio.run(_run())
    except (ValueError, OSError) as exc:
        raise CyberCommandError(f"{type(exc).__name__}: {exc}") from exc

    if as_json:
        console.print_json(json.dumps(run.to_dict(), default=str))
        return

    console.print(f"[success]{len(run.findings)} findings after verification[/success]")
    console.print(f"[dim]plan: {len(run.plan.lines)} lines of attack[/dim]")
    for line in run.plan.lines:
        console.print(f"[dim]  • {line.name}: {line.intent[:90]}[/dim]")
    if run.plan.fallback:
        console.print(
            "[warning]The engine's default plan was used (no planner available).[/warning]"
        )
    if len(run.waves) > 1:
        summary = ", ".join(
            f"wave {wave.index}: +{wave.new_findings}" for wave in run.waves
        )
        console.print(f"[dim]{len(run.waves)} waves ({summary})[/dim]")
    _print_histogram(run.stats.findings_by_severity)
    _print_findings(run.findings)

    if run.verification.dismissed:
        console.print(f"[dim]{len(run.verification.dismissed)} finding(s) dismissed:[/dim]")
        for dismissal in run.verification.dismissed:
            console.print(f"[dim]  − {dismissal.title}: {dismissal.reason[:110]}[/dim]")

    if report_dir:
        from aegisx_agent.cyber.report import write_reports

        written = write_reports(run.to_scan_result(), report_dir)
        for name, path in written.items():
            console.print(f"[success]📄 {name}: {path}[/success]")


def render_report(
    source: str,
    *,
    directory: str = "aegisx-report",
    report_format: str = "all",
    minimum_severity: str = "info",
) -> None:
    """Re-render a saved findings file into a different report format."""
    from aegisx_agent.cyber.models import Finding, ScanResult
    from aegisx_agent.cyber.report import ReportOptions, write_reports

    path = Path(source).expanduser()
    if not path.exists():
        raise CyberCommandError(f"File not found: {path}")

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CyberCommandError(f"Could not read {path}: {exc}") from exc

    entries = payload.get("findings", []) if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise CyberCommandError("The file does not contain a findings list.")

    result = ScanResult(
        target=str(payload.get("target", path.stem)) if isinstance(payload, dict) else path.stem
    )
    for entry in entries:
        if isinstance(entry, dict):
            result.findings.append(Finding.from_dict(entry))
    result.finalize()

    options = ReportOptions(minimum_severity=_severity_filter(minimum_severity))
    written = write_reports(result, directory, options=options, formats=_formats(report_format))
    for name, report_path in written.items():
        console.print(f"[success]📄 {name}: {report_path}[/success]")


def show_capabilities() -> None:
    """List the capabilities the engine can execute."""
    from aegisx_agent.cyber.agent import CAPABILITY_CATALOGUE

    table = Table(title="Cyber capabilities", border_style="magenta")
    table.add_column("Capability", style="bold")
    table.add_column("Produces", style="dim")
    table.add_column("What it does")
    for name, (description, categories) in sorted(CAPABILITY_CATALOGUE.items()):
        table.add_row(name, ", ".join(c.value for c in categories), description)
    console.print(table)


# ═══════════════════════════════════════════════════
#  TYPER COMMANDS
# ═══════════════════════════════════════════════════


def cyber_scan(
    target: str = typer.Argument(..., help="Target URL to assess (must be authorised)"),
    deep: bool = typer.Option(True, "--deep/--no-deep", help="Run the injection checks too"),
    max_requests: int = typer.Option(2000, "--max-requests", help="Request budget"),
    rate: float = typer.Option(10.0, "--rate", help="Requests per second"),
    concurrency: int = typer.Option(6, "--concurrency", help="Concurrent requests"),
    timeout: float = typer.Option(1800.0, "--timeout", help="Deadline in seconds"),
    report_dir: str = typer.Option(
        "", "--report-dir", help="Write SARIF/Markdown/HTML reports into this directory"
    ),
    report_format: str = typer.Option(
        "all", "--report-format", help="sarif, markdown, html, or all"
    ),
    minimum_severity: str = typer.Option(
        "info", "--min-severity", help="Lowest severity to show: info|low|medium|high|critical"
    ),
    allow_private: bool = typer.Option(
        False,
        "--allow-private",
        help="Permit loopback and private addresses (own network only)",
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON"),
) -> None:
    """Run a deterministic security scan against an authorised target."""
    try:
        run_scan(
            target,
            deep=deep,
            max_requests=max_requests,
            rate=rate,
            concurrency=concurrency,
            timeout=timeout,
            report_dir=report_dir,
            report_format=report_format,
            minimum_severity=minimum_severity,
            allow_private=allow_private,
            as_json=as_json,
        )
    except CyberCommandError as exc:
        console.print(f"[error]Scan failed: {exc}[/error]")
        raise typer.Exit(code=1) from exc


def cyber_sast(
    path: str = typer.Argument(".", help="Directory or file to analyse"),
    report_dir: str = typer.Option("", "--report-dir", help="Write the findings out as a report"),
    report_format: str = typer.Option(
        "all", "--report-format", help="sarif, markdown, html, or all"
    ),
    minimum_severity: str = typer.Option("info", "--min-severity", help="Lowest severity to show"),
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON"),
) -> None:
    """Statically analyse a source tree for secrets, sinks, and misconfiguration."""
    try:
        run_sast(
            path,
            report_dir=report_dir,
            report_format=report_format,
            minimum_severity=minimum_severity,
            as_json=as_json,
        )
    except CyberCommandError as exc:
        console.print(f"[error]Static analysis failed: {exc}[/error]")
        raise typer.Exit(code=1) from exc


def cyber_audit(
    path: str = typer.Argument(".", help="Project root to read manifests from"),
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON"),
) -> None:
    """Check a project's dependencies against the advisory database."""
    try:
        run_audit(path, as_json=as_json)
    except CyberCommandError as exc:
        console.print(f"[error]Audit failed: {exc}[/error]")
        raise typer.Exit(code=1) from exc


def cyber_swarm(
    target: str = typer.Argument(..., help="Target URL to assess (must be authorised)"),
    max_requests: int = typer.Option(2000, "--max-requests", help="Request budget"),
    concurrency: int = typer.Option(4, "--concurrency", help="Concurrent lines of attack"),
    timeout: float = typer.Option(1800.0, "--timeout", help="Deadline in seconds"),
    report_dir: str = typer.Option(
        "", "--report-dir", help="Write SARIF/Markdown/HTML reports into this directory"
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON"),
) -> None:
    """Run several lines of attack concurrently, without the model."""
    try:
        run_swarm(
            target,
            max_requests=max_requests,
            concurrency=concurrency,
            timeout=timeout,
            report_dir=report_dir,
            as_json=as_json,
        )
    except CyberCommandError as exc:
        console.print(f"[error]Swarm failed: {exc}[/error]")
        raise typer.Exit(code=1) from exc


def cyber_agent(
    target: str = typer.Argument(..., help="Target URL to assess (must be authorised)"),
    max_requests: int = typer.Option(2000, "--max-requests", help="Request budget"),
    concurrency: int = typer.Option(4, "--concurrency", help="Concurrent lines of attack"),
    timeout: float = typer.Option(1800.0, "--timeout", help="Deadline in seconds"),
    verify: bool = typer.Option(
        True, "--verify/--no-verify", help="Let the model review and dismiss false positives"
    ),
    report_dir: str = typer.Option(
        "", "--report-dir", help="Write SARIF/Markdown/HTML reports into this directory"
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON"),
) -> None:
    """Run the autonomous assessment: survey, plan, execute, verify, report."""
    try:
        run_agent(
            target,
            max_requests=max_requests,
            concurrency=concurrency,
            timeout=timeout,
            verify=verify,
            report_dir=report_dir,
            as_json=as_json,
        )
    except CyberCommandError as exc:
        console.print(f"[error]Assessment failed: {exc}[/error]")
        raise typer.Exit(code=1) from exc


def cyber_report(
    source: str = typer.Argument(..., help="A findings JSON file to re-render"),
    directory: str = typer.Option("aegisx-report", "--dir", help="Output directory"),
    report_format: str = typer.Option("all", "--format", help="sarif, markdown, html, or all"),
    minimum_severity: str = typer.Option("info", "--min-severity", help="Lowest severity to show"),
) -> None:
    """Re-render a saved findings file into a different report format."""
    try:
        render_report(
            source,
            directory=directory,
            report_format=report_format,
            minimum_severity=minimum_severity,
        )
    except CyberCommandError as exc:
        console.print(f"[error]Report failed: {exc}[/error]")
        raise typer.Exit(code=1) from exc


def cyber_capabilities() -> None:
    """List the capabilities the engine can execute."""
    show_capabilities()


# ═══════════════════════════════════════════════════
#  INTERACTIVE SLASH COMMAND (/cyber ...)
# ═══════════════════════════════════════════════════

CYBER_USAGE = """[bold]Cyber commands[/bold]

  /cyber scan <url>          Run the deterministic scan against an authorised target
  /cyber sast [path]         Statically analyse a source tree (default: current directory)
  /cyber audit [path]        Check dependencies against the advisory database
  /cyber swarm <url>         Run several lines of attack concurrently
  /cyber agent <url>         Run the autonomous assessment (survey → plan → verify)
  /cyber capabilities        List the capabilities the engine can execute
  /cyber status              Show the cyber configuration and the authorised scope
  /cyber scope <url>         Show what the scope guard would allow for a target"""


def _handle_cyber_command(args: str, agent: Any) -> None:
    """Handle ``/cyber`` in the interactive session."""
    parts = args.strip().split(maxsplit=1)
    if not parts:
        console.print(CYBER_USAGE)
        return

    action = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    try:
        match action:
            case "scan":
                if not rest:
                    console.print("[error]Usage: /cyber scan <url>[/error]")
                    return
                run_scan(rest)
            case "sast":
                run_sast(rest or ".")
            case "audit":
                run_audit(rest or ".")
            case "swarm":
                if not rest:
                    console.print("[error]Usage: /cyber swarm <url>[/error]")
                    return
                run_swarm(rest)
            case "agent":
                if not rest:
                    console.print("[error]Usage: /cyber agent <url>[/error]")
                    return
                run_agent(rest)
            case "capabilities":
                show_capabilities()
            case "status":
                _print_cyber_status(agent)
            case "scope":
                if not rest:
                    console.print("[error]Usage: /cyber scope <url>[/error]")
                    return
                _print_scope(rest)
            case "help":
                console.print(CYBER_USAGE)
            case _:
                console.print(f"[error]Unknown cyber command '{action}'.[/error]")
                console.print(CYBER_USAGE)
    except CyberCommandError as exc:
        console.print(f"[error]{exc}[/error]")


def _print_cyber_status(agent: Any) -> None:
    """Show the cyber configuration and the tools the agent has."""
    config = getattr(agent, "config", None)
    enabled = bool(getattr(config, "cyber_enabled", False))
    table = Table(title="🛡️ Cyber mode", border_style="magenta")
    table.add_column("Setting", style="bold")
    table.add_column("Value")
    table.add_row("Enabled", "yes" if enabled else "no")
    table.add_row(
        "Authorised targets",
        ", ".join(getattr(config, "cyber_target_names", [])) or "(named per request)",
    )
    table.add_row("Request budget", str(getattr(config, "cyber_max_requests", "?")))
    table.add_row("Rate limit", f"{getattr(config, 'cyber_requests_per_second', '?')} req/s")
    table.add_row("Concurrency", str(getattr(config, "cyber_max_concurrency", "?")))
    table.add_row(
        "Private addresses",
        "allowed" if getattr(config, "cyber_allow_private_addresses", False) else "refused",
    )
    console.print(table)

    registry = getattr(agent, "tools", None)
    lister = getattr(registry, "list_tools", None)
    tools = [tool.name for tool in lister() if tool.name.startswith("cyber_")] if lister else []
    if tools:
        console.print(f"[dim]Cyber tools registered: {', '.join(tools)}[/dim]")
    else:
        console.print(
            "[dim]No cyber tools are registered in this session "
            "(AEGISX_CYBER_ENABLED is false), so the model cannot drive a "
            "scan by itself. The commands below still work.[/dim]"
        )


def _print_scope(target: str) -> None:
    """Show what the scope guard would allow for a target."""
    from aegisx_agent.cyber.scope import ScopeGuard, scope_from_targets

    try:
        policy = scope_from_targets([target])
    except ValueError as exc:
        console.print(f"[error]{exc}[/error]")
        return

    guard = ScopeGuard(policy)
    base = target if "://" in target else f"https://{target}"
    decision = guard.check(base)
    table = Table(title="Authorised scope", border_style="magenta")
    table.add_column("Property", style="bold")
    table.add_column("Value")
    table.add_row("Host", ", ".join(policy.allowed_hosts))
    table.add_row("Ports", ", ".join(str(port) for port in policy.allowed_ports))
    table.add_row("Paths", ", ".join(policy.allowed_paths) or "(any path)")
    table.add_row("Base URL", "allowed" if decision.allowed else f"refused — {decision.reason}")
    table.add_row("Request budget", str(policy.max_requests))
    table.add_row("Rate limit", f"{policy.requests_per_second} req/s")
    table.add_row("Deadline", f"{policy.timeout_seconds}s")
    console.print(table)


def _cyber_model() -> Any:
    """The LLM the cyber agent plans and reviews with, if one is available.

    Returns ``None`` when no provider is configured. The cyber agent
    handles that: it falls back to the engine's default plan and skips
    the review stage rather than failing the run.
    """
    from aegisx_agent.cli.main import _get_agent, _get_config
    from aegisx_agent.core.config import missing_credentials

    try:
        config = _get_config()
    except Exception:  # noqa: BLE001 - a bad config must not stop the scan
        return None
    if missing_credentials(config):
        return None
    try:
        return _get_agent(config).llm
    except Exception:  # noqa: BLE001 - the deterministic path still works
        return None
