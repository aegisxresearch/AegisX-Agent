"""Reporting — turning findings into something a human or a CI system reads.

Three formats, three readers:

- :class:`SarifReporter` — SARIF 2.1.0, the format GitHub code
  scanning, GitLab, and most CI secret-scanning integrations accept.
  It is the only one of the three that a pipeline can act on
  automatically.
- :class:`MarkdownReporter` — for a pull request comment, a wiki page,
  or an attachment to a ticket.
- :class:`HtmlReporter` — a single self-contained file with no
  external assets, so it can be emailed or attached and still render
  correctly in six months.

All three read from the same source of truth, so they cannot disagree
about what was found. What they *can* do differently is emphasis: the
HTML report groups by severity and shows evidence inline, the Markdown
report is a flat list that diffs well, and SARIF is a machine contract
with no prose at all.
"""

from __future__ import annotations

import html
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from aegisx_agent.cyber.models import (
    Category,
    Confidence,
    Finding,
    ScanResult,
    Severity,
)

#: SARIF severity values, mapped from ours.
_SARIF_LEVEL: dict[Severity, str] = {
    Severity.CRITICAL: "error",
    Severity.HIGH: "error",
    Severity.MEDIUM: "warning",
    Severity.LOW: "note",
    Severity.INFO: "note",
}

#: SARIF confidence is a 0-1 double.
_SARIF_CONFIDENCE: dict[Confidence, float] = {
    Confidence.CONFIRMED: 1.0,
    Confidence.PROBABLE: 0.6,
    Confidence.TENTATIVE: 0.25,
}

#: SARIF rule ids are stable identifiers. The category plus the
#: finding's source makes a readable and stable key.
_SARIF_RULE_ID = {
    Category.RECON: "AEGISX-RECON",
    Category.HEADERS: "AEGISX-HEADERS",
    Category.COOKIE: "AEGISX-COOKIE",
    Category.CORS: "AEGISX-CORS",
    Category.EXPOSURE: "AEGISX-EXPOSURE",
    Category.INJECTION: "AEGISX-INJECTION",
    Category.AUTHZ: "AEGISX-AUTHZ",
    Category.SECRETS: "AEGISX-SECRETS",
    Category.CRYPTO: "AEGISX-CRYPTO",
    Category.CONFIG: "AEGISX-CONFIG",
    Category.TRANSPORT: "AEGISX-TRANSPORT",
    Category.SUPPLY_CHAIN: "AEGISX-SUPPLY-CHAIN",
    Category.CODE: "AEGISX-CODE",
    Category.INFO: "AEGISX-INFO",
}

#: Tool metadata. The version is the package version.
TOOL_NAME = "AegisX-Cyber"
TOOL_URI = "https://github.com/aegisx/aegisx-agent"
SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"


def _sarif_rule_id(finding: Finding) -> str:
    """A stable rule identifier for a finding."""
    base = _SARIF_RULE_ID.get(finding.category, "AEGISX-FINDING")
    if finding.source:
        suffix = finding.source.split(":")[-1].replace("_", "-").upper()
        return f"{base}/{suffix}"
    return base


def _sarif_message(finding: Finding) -> str:
    """The one-line description SARIF shows next to the result."""
    if finding.description:
        return finding.description
    return finding.title


@dataclass
class ReportOptions:
    """Knobs the reporters share."""

    title: str = "AegisX Security Assessment"
    include_evidence: bool = True
    include_info: bool = True
    minimum_severity: Severity = Severity.INFO
    redact_evidence: bool = True


def _visible(findings: Iterable[Finding], options: ReportOptions) -> list[Finding]:
    """The findings a report should show, worst first."""
    selected = [
        finding
        for finding in findings
        if finding.severity.weight >= options.minimum_severity.weight
        and (options.include_info or finding.severity is not Severity.INFO)
    ]
    return sorted(
        selected,
        key=lambda finding: (-finding.severity.weight, finding.title.lower()),
    )


class SarifReporter:
    """SARIF 2.1.0 — the format a CI pipeline can consume.

    The output follows the specification's required shape: a run with a
    tool driver, a rules array, and a results array. Every result
    points at a rule, and every rule carries the metadata a code
    scanning UI needs to group and explain the finding.
    """

    def __init__(self, options: ReportOptions | None = None) -> None:
        self.options = options or ReportOptions()

    def build(self, result: ScanResult) -> dict[str, Any]:
        """Build the SARIF document for a scan result."""
        findings = _visible(result.findings, self.options)
        rules = self._build_rules(findings)
        rule_index = {rule["id"]: rule for rule in rules}

        sarif_results: list[dict[str, Any]] = []
        for finding in findings:
            sarif_results.append(self._build_result(finding, rule_index))

        return {
            "$schema": SARIF_SCHEMA,
            "version": "2.1.0",
            "runs": [
                {
                    "tool": {
                        "driver": {
                            "name": TOOL_NAME,
                            "informationUri": TOOL_URI,
                            "version": "1.0.0",
                            "rules": rules,
                        }
                    },
                    "invocations": [
                        {
                            "executionSuccessful": not result.stats.errors,
                            "endTimeUtc": result.finished_at
                            or datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "properties": {
                                "target": result.target,
                                "scope": result.scope,
                                "requestsSent": result.stats.requests_sent,
                                "endpointsCrawled": result.stats.endpoints_crawled,
                            },
                        }
                    ],
                    "results": sarif_results,
                }
            ],
        }

    def _build_rules(self, findings: list[Finding]) -> list[dict[str, Any]]:
        """One rule per distinct finding source."""
        rules: dict[str, dict[str, Any]] = {}
        for finding in findings:
            rule_id = _sarif_rule_id(finding)
            if rule_id in rules:
                continue
            rules[rule_id] = {
                "id": rule_id,
                "name": finding.source.split(":")[-1].replace("-", " ") or finding.category.value,
                "shortDescription": {"text": finding.title},
                "fullDescription": {"text": finding.description or finding.title},
                "help": {
                    "text": finding.remediation or "Review and remediate the finding.",
                    "markdown": (finding.remediation or "Review and remediate the finding."),
                },
                "defaultConfiguration": {
                    "level": _SARIF_LEVEL.get(finding.severity, "note"),
                },
                "properties": {
                    "category": finding.category.value,
                    "cwe": finding.cwe,
                    "owasp": finding.owasp,
                    "tags": list(finding.tags),
                    "security-severity": str(finding.cvss or ""),
                },
            }
        return list(rules.values())

    def _build_result(
        self, finding: Finding, rule_index: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        """One SARIF result for one finding."""
        rule_id = _sarif_rule_id(finding)
        rule = rule_index.get(rule_id, {})

        location: dict[str, Any] = {}
        if finding.endpoint:
            location = {
                "physicalLocation": {
                    "artifactLocation": {"uri": finding.endpoint},
                    "region": {"startLine": 1},
                }
            }

        result: dict[str, Any] = {
            "ruleId": rule_id,
            "level": _SARIF_LEVEL.get(finding.severity, "note"),
            "message": {"text": _sarif_message(finding)},
            "partialFingerprints": {"aegisxFingerprint": finding.fingerprint},
            "properties": {
                "severity": finding.severity.value,
                "confidence": finding.confidence.value,
                "category": finding.category.value,
                "cvss": finding.cvss,
                "cwe": finding.cwe,
                "owasp": finding.owasp,
                "tags": list(finding.tags),
                "source": finding.source,
            },
        }
        if location:
            result["locations"] = [location]
        if rule.get("help"):
            result["help"] = rule["help"]
        if self.options.include_evidence and finding.evidence:
            result["attachments"] = [
                {
                    "description": {"text": item.summary},
                    "text": {"text": self._evidence_text(item.snippet, item.request)},
                }
                for item in finding.evidence
            ]
        return result

    def _evidence_text(self, snippet: str, request: str) -> str:
        """The evidence body attached to a SARIF result."""
        parts: list[str] = []
        if request:
            parts.append(request)
        if snippet:
            parts.append(snippet[:2000])
        return "\n\n".join(parts)

    def render(self, result: ScanResult) -> str:
        """The SARIF document as JSON text."""
        return json.dumps(self.build(result), indent=2, ensure_ascii=False, default=str)


class MarkdownReporter:
    """A Markdown report — for a pull request, a ticket, or a wiki."""

    def __init__(self, options: ReportOptions | None = None) -> None:
        self.options = options or ReportOptions()

    def render(self, result: ScanResult) -> str:
        """The report as Markdown text."""
        findings = _visible(result.findings, self.options)
        lines: list[str] = []

        lines.append(f"# {self.options.title}")
        lines.append("")
        lines.append(f"**Target:** `{result.target}`")
        if result.scope:
            lines.append(f"**Scope:** `{result.scope}`")
        lines.append(f"**Generated:** {result.finished_at or result.started_at}")
        lines.append("")

        lines.extend(self._summary(result, findings))
        lines.append("")

        if not findings:
            lines.append("No findings were reported.")
            lines.append("")
            return "\n".join(lines)

        lines.append("## Findings")
        lines.append("")
        for index, finding in enumerate(findings, start=1):
            lines.extend(self._finding(finding, index))

        lines.extend(self._coverage(result))
        return "\n".join(lines)

    def _summary(self, result: ScanResult, findings: list[Finding]) -> list[str]:
        """The severity histogram and the work that produced it."""
        lines = ["## Summary", ""]
        histogram = result.stats.findings_by_severity or {}
        for severity in (
            Severity.CRITICAL,
            Severity.HIGH,
            Severity.MEDIUM,
            Severity.LOW,
            Severity.INFO,
        ):
            count = histogram.get(severity.value, 0)
            if not count and severity is Severity.INFO and not self.options.include_info:
                continue
            marker = "!" if severity.weight >= Severity.HIGH.weight else "-"
            lines.append(f"- {marker} **{severity.label}**: {count}")
        lines.append("")

        confirmed = sum(1 for item in findings if item.confidence is Confidence.CONFIRMED)
        lines.append(
            f"**{len(findings)} findings** across {len(result.hosts)} hosts and "
            f"{result.stats.endpoints_crawled} crawled endpoints. "
            f"{confirmed} were directly confirmed; the rest are signals that "
            "need a human to verify."
        )
        return lines

    def _finding(self, finding: Finding, index: int) -> list[str]:
        """One finding rendered as Markdown."""
        lines: list[str] = []
        lines.append(
            f"### {index}. {finding.title} — {finding.severity.label} ({finding.confidence.value})"
        )
        lines.append("")
        if finding.endpoint:
            lines.append(f"- **Location:** `{finding.endpoint}`")
        if finding.cwe:
            lines.append(f"- **CWE:** {finding.cwe}")
        if finding.owasp:
            lines.append(f"- **OWASP:** {finding.owasp}")
        if finding.cvss:
            lines.append(f"- **CVSS:** {finding.cvss}")
        if finding.references:
            links = ", ".join(f"[link]({ref})" for ref in finding.references[:3])
            lines.append(f"- **References:** {links}")
        lines.append("")

        if finding.description:
            lines.append(finding.description)
            lines.append("")
        if finding.impact:
            lines.append(f"**Impact.** {finding.impact}")
            lines.append("")
        if finding.remediation:
            lines.append(f"**Remediation.** {finding.remediation}")
            lines.append("")

        if self.options.include_evidence and finding.evidence:
            lines.append("<details><summary>Evidence</summary>")
            lines.append("")
            for item in finding.evidence:
                lines.append(f"- {item.summary}")
                body = item.snippet or item.request
                if body:
                    lines.append("")
                    lines.append("```")
                    lines.append(body[:1500])
                    lines.append("```")
                lines.append("")
            lines.append("</details>")
            lines.append("")
        return lines

    def _coverage(self, result: ScanResult) -> list[str]:
        """A note on what was scanned — a report that hides its gaps lies."""
        lines = ["## Coverage", ""]
        stats = result.stats
        lines.append(f"- Requests sent: {stats.requests_sent}")
        lines.append(f"- Endpoints crawled: {stats.endpoints_crawled}")
        lines.append(f"- Checks run: {stats.checks_run}")
        lines.append(f"- Hosts scanned: {stats.hosts_scanned}")
        if stats.errors:
            lines.append(f"- Errors: {len(stats.errors)}")
            for error in stats.errors[:10]:
                lines.append(f"  - `{error}`")
        lines.append("")
        return lines


class HtmlReporter:
    """A single-file HTML report.

    No CDN, no fonts, no scripts. The file is self-contained so it
    renders identically in a browser, in a mail client, and in an
    archive five years from now.
    """

    def __init__(self, options: ReportOptions | None = None) -> None:
        self.options = options or ReportOptions()

    def render(self, result: ScanResult) -> str:
        """The report as one HTML document."""
        findings = _visible(result.findings, self.options)
        escaped_title = html.escape(self.options.title)
        escaped_target = html.escape(result.target)

        parts: list[str] = [
            "<!DOCTYPE html>",
            '<html lang="en"><head><meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>{escaped_title}</title>",
            "<style>",
            self._styles(),
            "</style></head><body>",
            '<main class="sheet">',
            f"<h1>{escaped_title}</h1>",
            '<dl class="meta">',
            f"<div><dt>Target</dt><dd><code>{escaped_target}</code></dd></div>",
        ]
        if result.scope:
            parts.append(
                f"<div><dt>Scope</dt><dd><code>{html.escape(result.scope)}</code></dd></div>"
            )
        parts.append(
            f"<div><dt>Generated</dt><dd>{html.escape(result.finished_at or result.started_at)}"
            "</dd></div>"
        )
        parts.append("</dl>")

        parts.extend(self._summary(result, findings))

        if not findings:
            parts.append("<p class='empty'>No findings were reported.</p>")
        else:
            parts.append('<section class="findings">')
            for index, finding in enumerate(findings, start=1):
                parts.extend(self._finding(finding, index))
            parts.append("</section>")

        parts.extend(self._coverage(result))
        parts.append("</main></body></html>")
        return "".join(parts)

    def _summary(self, result: ScanResult, findings: list[Finding]) -> list[str]:
        """The severity histogram as a bar row."""
        histogram = result.stats.findings_by_severity or {}
        total = max(1, len(findings))
        parts = ['<section class="summary"><h2>Summary</h2><div class="bars">']
        for severity in (
            Severity.CRITICAL,
            Severity.HIGH,
            Severity.MEDIUM,
            Severity.LOW,
            Severity.INFO,
        ):
            count = histogram.get(severity.value, 0)
            width = (count / total) * 100.0
            parts.append(
                f'<div class="bar bar-{severity.value}">'
                f'<span class="bar-label">{severity.label}</span>'
                f'<span class="bar-count">{count}</span>'
                f'<span class="bar-fill" style="width:{width:.1f}%"></span>'
                "</div>"
            )
        parts.append("</div>")
        confirmed = sum(1 for item in findings if item.confidence is Confidence.CONFIRMED)
        parts.append(
            f'<p class="lede">{len(findings)} findings. {confirmed} directly confirmed; '
            "the rest are signals awaiting human verification.</p>"
        )
        parts.append("</section>")
        return parts

    def _finding(self, finding: Finding, index: int) -> list[str]:
        """One finding as an HTML article."""
        parts = [
            f'<article class="finding severity-{finding.severity.value}">',
            "<header>",
            f'<span class="sev">{finding.severity.label}</span>',
            f'<span class="conf">{html.escape(finding.confidence.value)}</span>',
            f"<h3>{index}. {html.escape(finding.title)}</h3>",
            "</header>",
        ]
        if finding.endpoint:
            parts.append(f"<p class='loc'><code>{html.escape(finding.endpoint)}</code></p>")

        meta: list[str] = []
        if finding.cwe:
            meta.append(f"CWE {finding.cwe}")
        if finding.owasp:
            meta.append(html.escape(finding.owasp))
        if finding.cvss:
            meta.append(f"CVSS {finding.cvss}")
        if meta:
            parts.append(f"<p class='meta-line'>{' · '.join(meta)}</p>")

        if finding.description:
            parts.append(f"<p>{html.escape(finding.description)}</p>")
        if finding.impact:
            parts.append(f"<p><strong>Impact.</strong> {html.escape(finding.impact)}</p>")
        if finding.remediation:
            parts.append(f"<p><strong>Remediation.</strong> {html.escape(finding.remediation)}</p>")

        if finding.references:
            links = " ".join(
                f'<a href="{html.escape(ref, quote=True)}" rel="noreferrer noopener">ref</a>'
                for ref in finding.references[:5]
            )
            parts.append(f"<p class='refs'>{links}</p>")

        if self.options.include_evidence and finding.evidence:
            parts.append("<details><summary>Evidence</summary>")
            for item in finding.evidence:
                parts.append(f"<p class='ev-sum'>{html.escape(item.summary)}</p>")
                body = item.snippet or item.request
                if body:
                    parts.append(f"<pre>{html.escape(body[:1500])}</pre>")
            parts.append("</details>")

        parts.append("</article>")
        return parts

    def _coverage(self, result: ScanResult) -> list[str]:
        """The coverage note."""
        stats = result.stats
        parts = ["<section class='coverage'><h2>Coverage</h2><ul>"]
        parts.append(f"<li>Requests sent: <strong>{stats.requests_sent}</strong></li>")
        parts.append(f"<li>Endpoints crawled: <strong>{stats.endpoints_crawled}</strong></li>")
        parts.append(f"<li>Checks run: <strong>{stats.checks_run}</strong></li>")
        parts.append(f"<li>Hosts scanned: <strong>{stats.hosts_scanned}</strong></li>")
        parts.append("</ul>")
        if stats.errors:
            parts.append("<h3>Errors</h3><ul>")
            for error in stats.errors[:20]:
                parts.append(f"<li><code>{html.escape(error)}</code></li>")
            parts.append("</ul>")
        parts.append("</section>")
        return parts

    @staticmethod
    def _styles() -> str:
        """The stylesheet — OKLCH tokens, no external fonts."""
        return """
:root {
  --bg: oklch(0.16 0.012 265);
  --surface: oklch(0.21 0.014 265);
  --line: oklch(0.32 0.018 265);
  --text: oklch(0.94 0.008 265);
  --muted: oklch(0.72 0.018 265);
  --critical: oklch(0.62 0.20 25);
  --high: oklch(0.70 0.17 55);
  --medium: oklch(0.78 0.14 85);
  --low: oklch(0.72 0.11 200);
  --info: oklch(0.66 0.06 265);
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 16px/1.65 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
}
.sheet { max-width: 62rem; margin: 0 auto; padding: 3rem 1.5rem 5rem; }
h1 { font-size: clamp(1.9rem, 1.3rem + 2.2vw, 2.9rem); margin: 0 0 1.5rem; letter-spacing: -0.02em; }
h2 { font-size: 1.15rem; text-transform: uppercase; letter-spacing: 0.09em; color: var(--muted); margin: 2.5rem 0 1rem; }
h3 { font-size: 1.05rem; margin: 0 0 .5rem; line-height: 1.4; }
code { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace; font-size: .88em; }
.meta { display: flex; flex-wrap: wrap; gap: 2rem; margin: 0 0 2rem; padding: 1.1rem 1.25rem; background: var(--surface); border: 1px solid var(--line); border-radius: 10px; }
.meta div { display: flex; flex-direction: column; gap: .2rem; }
.meta dt { font-size: .68rem; text-transform: uppercase; letter-spacing: .1em; color: var(--muted); }
.meta dd { margin: 0; }
.bars { display: flex; flex-direction: column; gap: .45rem; }
.bar { position: relative; display: grid; grid-template-columns: 5.5rem 2.5rem 1fr; align-items: center; gap: .75rem; padding: .5rem .75rem; background: var(--surface); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
.bar-label { font-size: .72rem; letter-spacing: .09em; font-weight: 700; }
.bar-count { font-variant-numeric: tabular-nums; color: var(--muted); }
.bar-fill { position: absolute; inset: 0 auto 0 0; opacity: .16; }
.bar-critical .bar-label { color: var(--critical); }
.bar-critical .bar-fill { background: var(--critical); }
.bar-high .bar-label { color: var(--high); }
.bar-high .bar-fill { background: var(--high); }
.bar-medium .bar-label { color: var(--medium); }
.bar-medium .bar-fill { background: var(--medium); }
.bar-low .bar-label { color: var(--low); }
.bar-low .bar-fill { background: var(--low); }
.bar-info .bar-label { color: var(--info); }
.bar-info .bar-fill { background: var(--info); }
.lede { color: var(--muted); margin: 1.1rem 0 0; }
.finding { background: var(--surface); border: 1px solid var(--line); border-left: 3px solid var(--info); border-radius: 10px; padding: 1.25rem 1.4rem; margin: 0 0 1.1rem; }
.finding.severity-critical { border-left-color: var(--critical); }
.finding.severity-high { border-left-color: var(--high); }
.finding.severity-medium { border-left-color: var(--medium); }
.finding.severity-low { border-left-color: var(--low); }
.finding header { display: flex; align-items: baseline; gap: .65rem; flex-wrap: wrap; }
.sev { font-size: .68rem; font-weight: 800; letter-spacing: .1em; padding: .18rem .5rem; border-radius: 5px; background: var(--line); }
.severity-critical .sev { background: var(--critical); color: oklch(0.16 0.012 265); }
.severity-high .sev { background: var(--high); color: oklch(0.16 0.012 265); }
.severity-medium .sev { background: var(--medium); color: oklch(0.16 0.012 265); }
.severity-low .sev { background: var(--low); color: oklch(0.16 0.012 265); }
.conf { font-size: .68rem; letter-spacing: .07em; color: var(--muted); text-transform: uppercase; }
.loc { margin: .55rem 0 .8rem; }
.meta-line { font-size: .8rem; color: var(--muted); margin: 0 0 .8rem; }
.refs a { display: inline-block; margin-right: .5rem; font-size: .75rem; color: var(--low); text-decoration: none; border: 1px solid var(--line); border-radius: 4px; padding: .1rem .45rem; }
details { margin-top: .9rem; border-top: 1px solid var(--line); padding-top: .8rem; }
summary { cursor: pointer; color: var(--muted); font-size: .82rem; }
.ev-sum { font-size: .85rem; margin: .7rem 0 .3rem; }
pre { background: var(--bg); border: 1px solid var(--line); border-radius: 7px; padding: .8rem; overflow-x: auto; font-size: .8rem; }
.coverage ul { padding-left: 1.2rem; }
.coverage li { margin: .25rem 0; }
.empty { color: var(--muted); }
@media (prefers-color-scheme: light) {
  :root { --bg: oklch(0.98 0.004 265); --surface: oklch(1 0 0); --line: oklch(0.88 0.008 265); --text: oklch(0.22 0.014 265); --muted: oklch(0.48 0.016 265); }
  pre { background: oklch(0.96 0.005 265); }
}
""".strip()


def write_reports(
    result: ScanResult,
    directory: str,
    *,
    options: ReportOptions | None = None,
    formats: tuple[str, ...] = ("sarif", "markdown", "html"),
) -> dict[str, str]:
    """Write the requested report formats into ``directory``.

    Returns a mapping of format name to the file that was written.
    """
    from pathlib import Path

    options = options or ReportOptions()
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)

    written: dict[str, str] = {}
    if "sarif" in formats:
        path = target / "aegisx-report.sarif"
        path.write_text(SarifReporter(options).render(result), encoding="utf-8")
        written["sarif"] = str(path)
    if "markdown" in formats:
        path = target / "aegisx-report.md"
        path.write_text(MarkdownReporter(options).render(result), encoding="utf-8")
        written["markdown"] = str(path)
    if "html" in formats:
        path = target / "aegisx-report.html"
        path.write_text(HtmlReporter(options).render(result), encoding="utf-8")
        written["html"] = str(path)
    return written
