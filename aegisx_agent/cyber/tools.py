"""Cyber tools — the security engine exposed to the agent as callable tools.

These are the tools a general-purpose agent can call when the operator
turns on cyber mode. They are ordinary :class:`~aegisx_agent.tools.base.Tool`
implementations, so they inherit the permission gate, the audit log, and
the schema plumbing the rest of the project already has.

Two of them are marked ``DANGEROUS``:

- ``cyber_scan`` sends traffic to a host that is not the operator's
  machine. It stays inside a scope the operator stated, but it is
  still traffic to somebody else's server, so it asks first unless the
  operator has allow-listed it.
- ``cyber_sast`` reads the source tree. It is read-only, but it walks
  the filesystem, so it is marked ``CAUTION`` rather than ``SAFE``.

``cyber_cve`` and ``cyber_report`` only read local manifests and write
local files, so they are ``SAFE``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aegisx_agent.tools.base import Tool, ToolResult, ToolRisk, ToolStatus


def _finding_digest(findings: list[Any], limit: int = 40) -> str:
    """A compact, readable digest of findings for the model's context."""
    if not findings:
        return "No findings."
    lines: list[str] = []
    for finding in findings[:limit]:
        severity = getattr(finding, "severity", None)
        severity_text = str(getattr(severity, "value", severity))
        endpoint = getattr(finding, "endpoint", "") or "-"
        title = getattr(finding, "title", "")
        lines.append(f"[{severity_text.upper()}] {title} — {endpoint}")
    if len(findings) > limit:
        lines.append(f"... and {len(findings) - limit} more")
    return "\n".join(lines)


class CyberSession:
    """The findings collected so far in one session.

    The scan, static-analysis, and advisory tools all write into one
    session, and the report tool reads from it. Without this the model
    would have to copy findings from one tool's output into another's
    arguments, which it does badly.
    """

    def __init__(self) -> None:
        self.findings: list[Any] = []

    def add(self, findings: list[Any]) -> None:
        """Record findings.

        A finding that repeats one already in the session is not
        appended twice: the session keeps one copy per fingerprint, so
        a second scan of the same target updates rather than doubles.
        """
        seen = {
            getattr(finding, "fingerprint", None)
            for finding in self.findings
            if getattr(finding, "fingerprint", None)
        }
        for finding in findings:
            fingerprint = getattr(finding, "fingerprint", None)
            if fingerprint and fingerprint in seen:
                continue
            if fingerprint:
                seen.add(fingerprint)
            self.findings.append(finding)

    def clear(self) -> None:
        """Drop every finding in the session."""
        self.findings.clear()


class CyberScanTool(Tool):
    """Run a scoped, rate-limited security scan against an authorised target."""

    def __init__(self, session: CyberSession) -> None:
        super().__init__(
            name="cyber_scan",
            description=(
                "Run an authorised security scan against a target you have been "
                "explicitly permitted to test. Maps the attack surface, then runs "
                "the web security checks (headers, cookies, TLS, CORS, exposure, "
                "injection, access control) against what it finds. The scan is "
                "bounded by a request budget and a deadline, and only the host "
                "you name is contacted. Use this when the operator asks you to "
                "assess, audit, or pen-test a site."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": (
                            "The base URL to scan, e.g. https://app.example.com. "
                            "Only this host is contacted."
                        ),
                    },
                    "deep": {
                        "type": "boolean",
                        "description": (
                            "Run the expensive injection checks as well as the "
                            "cheap configuration ones (default: true)"
                        ),
                        "default": True,
                    },
                    "max_requests": {
                        "type": "integer",
                        "description": "Request budget for the whole scan (default: 800)",
                        "default": 800,
                    },
                    "enumerate_subdomains": {
                        "type": "boolean",
                        "description": (
                            "Resolve common subdomains and report the ones that "
                            "exist but sit outside the authorised scope (default: false)"
                        ),
                        "default": False,
                    },
                    "scan_ports": {
                        "type": "boolean",
                        "description": "Probe common TCP ports (default: false)",
                        "default": False,
                    },
                },
                "required": ["target"],
            },
            risk=ToolRisk.DANGEROUS,
        )
        self.session = session

    async def execute(self, **kwargs: Any) -> ToolResult:
        from aegisx_agent.cyber.scanner import ScanConfig, Scanner, summarise

        target = str(kwargs.get("target", "")).strip()
        if not target:
            return ToolResult(status=ToolStatus.ERROR, output="", error="target is required")
        if "://" not in target:
            target = f"https://{target}"

        config = ScanConfig(
            targets=[target],
            deep=bool(kwargs.get("deep", True)),
            max_requests=int(kwargs.get("max_requests", 800)),
            enumerate_subdomains=bool(kwargs.get("enumerate_subdomains", False)),
            scan_ports=bool(kwargs.get("scan_ports", False)),
        )
        scanner = Scanner(config)
        try:
            result = await scanner.run()
        except Exception as exc:  # noqa: BLE001 - a failed scan is a failed tool call
            return ToolResult(
                status=ToolStatus.ERROR,
                output="",
                error=f"scan failed: {type(exc).__name__}: {exc}",
            )

        self.session.add(result.findings)
        digest = _finding_digest(result.sorted_findings())
        output = (
            f"{summarise(result)}\n\n"
            f"Findings:\n{digest}\n\n"
            f"Coverage: {len(result.endpoints)} endpoints crawled, "
            f"{result.stats.checks_run} checks run, "
            f"{result.stats.requests_sent} requests sent."
        )
        return ToolResult(
            status=ToolStatus.SUCCESS,
            output=output,
            metadata={
                "target": result.target,
                "scope": result.scope,
                "findings": len(result.findings),
                "by_severity": result.stats.findings_by_severity,
                "requests_sent": result.stats.requests_sent,
            },
        )


class CyberSastTool(Tool):
    """Run static analysis over a source tree."""

    def __init__(self, session: CyberSession) -> None:
        super().__init__(
            name="cyber_sast",
            description=(
                "Statically analyse a source tree for committed secrets, dangerous "
                "call sites (command execution, deserialisation, SQL built by string "
                "concatenation, disabled TLS verification), weak cryptography, and "
                "framework misconfiguration. Read-only: nothing is executed and "
                "nothing is modified."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Directory or file to analyse (default: the current directory)"
                        ),
                        "default": ".",
                    },
                },
            },
            risk=ToolRisk.CAUTION,
        )
        self.session = session

    async def execute(self, **kwargs: Any) -> ToolResult:
        from aegisx_agent.cyber.sast import StaticAnalyzer

        raw_path = str(kwargs.get("path", ".") or ".")
        root = Path(raw_path).expanduser()
        if not root.exists():
            return ToolResult(
                status=ToolStatus.ERROR, output="", error=f"Path not found: {raw_path}"
            )

        analyzer = StaticAnalyzer()
        try:
            outcome = analyzer.analyze(root)
        except Exception as exc:  # noqa: BLE001 - a failed analysis is a failed tool call
            return ToolResult(
                status=ToolStatus.ERROR,
                output="",
                error=f"static analysis failed: {type(exc).__name__}: {exc}",
            )

        self.session.add(outcome.findings)
        digest = _finding_digest(outcome.findings)
        output = (
            f"Static analysis of {root}: {len(outcome.findings)} findings in "
            f"{outcome.files_scanned} files.\n\n"
            f"{digest}"
        )
        if outcome.errors:
            output += f"\n\nErrors: {len(outcome.errors)}"
        return ToolResult(
            status=ToolStatus.SUCCESS,
            output=output,
            metadata={
                "path": str(root),
                "findings": len(outcome.findings),
                "files_scanned": outcome.files_scanned,
            },
        )


class CyberCveTool(Tool):
    """Look up known advisories for a project's dependencies."""

    def __init__(self, session: CyberSession) -> None:
        super().__init__(
            name="cyber_cve",
            description=(
                "Check a project's dependencies against the OSV advisory database "
                "and report packages with known vulnerabilities. Falls back to a "
                "bundled offline knowledge base when the network is unavailable, "
                "so it always answers something."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Project root to read manifests from (default: current directory)"
                        ),
                        "default": ".",
                    },
                },
            },
            risk=ToolRisk.SAFE,
        )
        self.session = session

    async def execute(self, **kwargs: Any) -> ToolResult:
        from aegisx_agent.cyber.cve import VulnerabilityAuditor
        from aegisx_agent.cyber.sast import parse_dependencies

        raw_path = str(kwargs.get("path", ".") or ".")
        root = Path(raw_path).expanduser()
        if not root.exists():
            return ToolResult(
                status=ToolStatus.ERROR, output="", error=f"Path not found: {raw_path}"
            )

        dependencies = parse_dependencies(root)
        if not dependencies:
            return ToolResult(
                status=ToolStatus.SUCCESS,
                output=f"No dependency manifests were found under {root}.",
                metadata={"packages": 0},
            )

        auditor = VulnerabilityAuditor()
        try:
            report = await auditor.audit_dependencies(dependencies)
        except Exception as exc:  # noqa: BLE001 - a failed lookup is a failed tool call
            return ToolResult(
                status=ToolStatus.ERROR,
                output="",
                error=f"advisory lookup failed: {type(exc).__name__}: {exc}",
            )

        self.session.add(report.findings)
        digest = _finding_digest(report.findings)
        offline = " (offline knowledge base used)" if report.offline_used else ""
        output = (
            f"Checked {report.packages_checked} dependencies{offline}: "
            f"{len(report.findings)} advisories apply.\n\n"
            f"{digest}"
        )
        if report.errors:
            output += f"\n\nLookup errors: {len(report.errors)}"
        return ToolResult(
            status=ToolStatus.SUCCESS,
            output=output,
            metadata={
                "packages_checked": report.packages_checked,
                "advisories": len(report.advisories),
                "offline_used": report.offline_used,
            },
        )


class CyberReportTool(Tool):
    """Render the session's findings into a report file."""

    def __init__(self, session: CyberSession) -> None:
        super().__init__(
            name="cyber_report",
            description=(
                "Write a security report from findings you have already collected. "
                "Produces SARIF 2.1.0 (for CI), Markdown, and a self-contained "
                "HTML file. Uses the findings from the scans and analyses already "
                "performed in this session."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "Where to write the report files (default: ./aegisx-report)",
                        "default": "aegisx-report",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["sarif", "markdown", "html", "all"],
                        "description": "Which format to write (default: all)",
                        "default": "all",
                    },
                },
            },
            risk=ToolRisk.SAFE,
        )
        self.session = session

    async def execute(self, **kwargs: Any) -> ToolResult:
        from aegisx_agent.cyber.models import ScanResult
        from aegisx_agent.cyber.report import write_reports

        directory = str(kwargs.get("directory", "aegisx-report") or "aegisx-report")
        report_format = str(kwargs.get("format", "all") or "all")
        formats: tuple[str, ...]
        if report_format == "all":
            formats = ("sarif", "markdown", "html")
        else:
            formats = (report_format,)

        findings = list(self.session.findings)
        if not findings:
            return ToolResult(
                status=ToolStatus.ERROR,
                output="",
                error=("no findings are available to report. Run cyber_scan or cyber_sast first."),
            )

        result = ScanResult(target="aegisx assessment")
        result.findings = findings
        result.finalize()

        try:
            written = write_reports(result, directory, formats=formats)
        except OSError as exc:
            return ToolResult(
                status=ToolStatus.ERROR,
                output="",
                error=f"could not write the report: {exc}",
            )

        payload = json.dumps(written, indent=2)
        return ToolResult(
            status=ToolStatus.SUCCESS,
            output=f"Wrote {len(written)} report file(s):\n{payload}",
            metadata={"files": written, "findings": len(findings)},
        )


def register_cyber_tools(registry: Any, session: CyberSession | None = None) -> CyberSession:
    """Register the cyber tools on a tool registry.

    All four tools share one session, so a scan followed by a report
    works without the model having to carry findings between calls.
    Returns the session so the caller can inspect what was collected.
    """
    from aegisx_agent.tools.registry import ToolRegistry

    if not isinstance(registry, ToolRegistry):
        raise TypeError("registry must be a ToolRegistry")

    shared = session or CyberSession()
    registry.register(CyberScanTool(shared))
    registry.register(CyberSastTool(shared))
    registry.register(CyberCveTool(shared))
    registry.register(CyberReportTool(shared))
    return shared
