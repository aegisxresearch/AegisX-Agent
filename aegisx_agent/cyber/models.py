"""Domain model for AegisX Cyber — findings, evidence, targets, scan results.

Everything the scanner produces is one of these types. The model is
deliberately plain (dataclasses + enums, no pydantic) so it can be
serialised to JSON, embedded in prompts, and written to disk without
dragging a validation layer into the hot path of a scan.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Severity(str, Enum):
    """How bad a finding is, ordered from harmless to urgent."""

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def weight(self) -> int:
        """Sort weight — higher is worse."""
        return _SEVERITY_WEIGHT[self]

    @property
    def label(self) -> str:
        """Uppercase label used in terminal and HTML output."""
        return self.value.upper()


_SEVERITY_WEIGHT: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


class Confidence(str, Enum):
    """How sure the scanner is that a finding is real.

    A security report is only as good as its precision. A scanner that
    shouts "critical" for every 500 is a scanner nobody reads, so every
    finding carries its own confidence and the reporter separates the two
    axes instead of collapsing them into one scary number.
    """

    CONFIRMED = "confirmed"  # verified by direct observation
    PROBABLE = "probable"  # strong signal, not directly confirmed
    TENTATIVE = "tentative"  # worth a human look, not worth a page


class Category(str, Enum):
    """Coarse grouping of findings, used for report sections and filtering."""

    RECON = "recon"
    HEADERS = "headers"
    COOKIE = "cookie"
    CORS = "cors"
    EXPOSURE = "exposure"
    INJECTION = "injection"
    AUTHZ = "authz"
    SECRETS = "secrets"
    CRYPTO = "crypto"
    CONFIG = "config"
    TRANSPORT = "transport"
    SUPPLY_CHAIN = "supply-chain"
    CODE = "code"
    INFO = "info"


class EvidenceKind(str, Enum):
    """The form an artifact takes, so a reader knows how to trust it."""

    REQUEST = "request"
    RESPONSE = "response"
    COMMAND = "command"
    FILE = "file"
    MATCH = "match"


@dataclass(frozen=True)
class Evidence:
    """One artifact backing a finding.

    A finding without evidence is an opinion. Every finding carries at
    least one piece of the actual request, response, or source line that
    produced it, so a reader can reproduce the result by hand.
    """

    kind: EvidenceKind
    summary: str
    snippet: str = ""
    request: str = ""
    response_status: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def render(self) -> str:
        """Human-readable block for the Markdown and HTML reports."""
        lines = [f"**{self.kind.value}** — {self.summary}"]
        if self.request:
            lines.append(f"```http\n{self.request.strip()}\n```")
        if self.snippet:
            lines.append(f"```\n{self.snippet.strip()}\n```")
        return "\n".join(lines)


@dataclass
class Finding:
    """A single security finding.

    ``fingerprint`` is a stable hash over the parts that make a finding
    *the same finding*: its category, the endpoint it lives on, and a
    normalised title. Two scans of the same target therefore produce two
    rows in the store, not two rows in the report, and a re-scan can
    report "still present" versus "fixed" instead of starting from zero.
    """

    title: str
    severity: Severity
    category: Category
    confidence: Confidence = Confidence.PROBABLE
    target: str = ""
    endpoint: str = ""
    description: str = ""
    impact: str = ""
    remediation: str = ""
    cwe: str = ""
    owasp: str = ""
    cvss: float | None = None
    references: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    source: str = ""
    tags: list[str] = field(default_factory=list)
    discovered_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.fingerprint:
            self.fingerprint = self.compute_fingerprint()

    def compute_fingerprint(self) -> str:
        """Stable identity of this finding across runs."""
        normalised_title = " ".join(self.title.lower().split())
        material = "|".join(
            [
                self.category.value,
                self.target.lower(),
                self.endpoint.lower(),
                normalised_title,
            ]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["severity"] = self.severity.value
        payload["confidence"] = self.confidence.value
        payload["category"] = self.category.value
        payload["evidence"] = [item.to_dict() for item in self.evidence]
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Finding:
        known = {key: value for key, value in payload.items() if key in cls.__dataclass_fields__}
        known["severity"] = Severity(payload.get("severity", "info"))
        known["confidence"] = Confidence(payload.get("confidence", "probable"))
        known["category"] = Category(payload.get("category", "info"))
        known["evidence"] = [
            Evidence(
                kind=EvidenceKind(item.get("kind", "match")),
                summary=item.get("summary", ""),
                snippet=item.get("snippet", ""),
                request=item.get("request", ""),
                response_status=item.get("response_status"),
            )
            for item in payload.get("evidence", [])
        ]
        return cls(**known)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False, default=str)

    def merge(self, other: Finding) -> None:
        """Fold a newer observation of the same finding into this one.

        A re-scan may confirm a tentative finding, or attach evidence that
        was missing the first time. Severity never silently drops: it is
        raised to the worse of the two.
        """
        if other.severity.weight > self.severity.weight:
            self.severity = other.severity
        if other.confidence is Confidence.CONFIRMED and self.confidence is not (
            Confidence.CONFIRMED
        ):
            self.confidence = other.confidence
        known = {item.summary for item in self.evidence}
        for item in other.evidence:
            if item.summary not in known:
                self.evidence.append(item)
        if not self.remediation and other.remediation:
            self.remediation = other.remediation
        if not self.impact and other.impact:
            self.impact = other.impact
        self.discovered_at = other.discovered_at


@dataclass
class Target:
    """Something the scanner is allowed to look at.

    A target is always a URL. Ports, schemes, and paths are carried
    separately so the scope guard can reason about them without parsing
    strings at every call site.
    """

    url: str
    host: str
    port: int
    scheme: str = "https"
    path: str = "/"
    label: str = ""

    def __post_init__(self) -> None:
        if not self.label:
            self.label = f"{self.scheme}://{self.host}"

    def join(self, path: str) -> str:
        """Absolute URL for a path relative to this target's base path."""
        if path.startswith(("http://", "https://")):
            return path
        base = self.path if self.path.endswith("/") else f"{self.path}/"
        return f"{self.scheme}://{self.host}:{self.port}{base}{path.lstrip('/')}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HostInfo:
    """What reconnaissance learned about one host."""

    host: str
    ip: str = ""
    scheme: str = "https"
    port: int = 443
    open: bool = False
    title: str = ""
    server: str = ""
    technologies: list[str] = field(default_factory=list)
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScanStats:
    """Counters that describe how much work a scan actually did."""

    hosts_discovered: int = 0
    hosts_scanned: int = 0
    endpoints_crawled: int = 0
    requests_sent: int = 0
    checks_run: int = 0
    findings_total: int = 0
    findings_by_severity: dict[str, int] = field(default_factory=dict)
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScanResult:
    """The complete outcome of one scan: findings plus what was covered."""

    target: str
    scope: str = ""
    findings: list[Finding] = field(default_factory=list)
    hosts: list[HostInfo] = field(default_factory=list)
    endpoints: list[str] = field(default_factory=list)
    stats: ScanStats = field(default_factory=ScanStats)
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    finished_at: str = ""

    def finalize(self) -> None:
        """Stamp the end time and refresh the severity histogram."""
        self.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        histogram: dict[str, int] = {}
        for finding in self.findings:
            histogram[finding.severity.value] = histogram.get(finding.severity.value, 0) + 1
        self.stats.findings_by_severity = histogram
        self.stats.findings_total = len(self.findings)

    def sorted_findings(self) -> list[Finding]:
        """Findings worst-first, then by confidence, then by title."""
        confidence_rank = {
            Confidence.CONFIRMED: 0,
            Confidence.PROBABLE: 1,
            Confidence.TENTATIVE: 2,
        }
        return sorted(
            self.findings,
            key=lambda finding: (
                -finding.severity.weight,
                confidence_rank[finding.confidence],
                finding.title.lower(),
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "scope": self.scope,
            "findings": [finding.to_dict() for finding in self.findings],
            "hosts": [host.to_dict() for host in self.hosts],
            "endpoints": list(self.endpoints),
            "stats": self.stats.to_dict(),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
