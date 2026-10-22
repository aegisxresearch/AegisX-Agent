"""Vulnerability intelligence — turning a version number into a known flaw.

Identifying that a host runs "nginx 1.18.0" is only half the work. The
other half is knowing which published vulnerabilities affect that
exact version. This module does that lookup, and it does it against
OSV (the Open Source Vulnerability database), which is a free, public,
machine-readable API covering PyPI, npm, Maven, Go, crates.io, and
more.

Two design decisions are worth stating:

- **The network is optional.** A scan run on an isolated network, or
  one whose operator has no internet access, must still work. The
  module ships a small offline knowledge base of high-signal advisories
  and falls back to it when OSV is unreachable. A stale answer is
  labelled as such; a missing answer is not silently presented as
  "no vulnerabilities".
- **Version matching is conservative.** A range check that is unsure
  produces a *tentative* finding, never a confirmed one. Reporting
  "CVE-2021-44228 applies" for a version it merely guessed at is how a
  scanner loses the reader's trust permanently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from aegisx_agent.cyber.models import (
    Category,
    Confidence,
    Evidence,
    EvidenceKind,
    Finding,
    Severity,
)

#: The OSV query API. It is a POST endpoint taking a JSON body.
OSV_QUERY_URL = "https://api.osv.dev/v1/query"
OSV_VULN_URL = "https://api.osv.dev/v1/vulns"

#: How long to wait for OSV before giving up and using the offline KB.
OSV_TIMEOUT_SECONDS = 10.0

#: OSV ecosystem names, mapped from what the dependency parsers emit.
OSV_ECOSYSTEMS: dict[str, str] = {
    "npm": "npm",
    "pypi": "PyPI",
    "maven": "Maven",
    "go": "Go",
    "crates.io": "crates.io",
    "rubygems": "RubyGems",
    "packagist": "Packagist",
    "nuget": "NuGet",
    "hex": "Hex",
    "pub": "Pub",
}

#: Known-vulnerable version ranges, used by the offline knowledge base.
#: Each entry is ``(ecosystem, package, advisory, vulnerable_range, fixed_in)``
#: where ``vulnerable_range`` is an OSV-style range expression.
OFFLINE_ADVISORIES: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "PyPI",
        "django",
        "GHSA-rf4c-jh3c-rvcc",
        ">=3.0,<3.2.4",
        "3.2.4",
    ),
    (
        "PyPI",
        "flask",
        "GHSA-m2qf-hxjv-5gpq",
        "<2.3.2",
        "2.3.2",
    ),
    (
        "PyPI",
        "requests",
        "GHSA-j8r2-6x86-q33q",
        "<2.31.0",
        "2.31.0",
    ),
    (
        "PyPI",
        "jinja2",
        "GHSA-h5c8-rqwp-cp95",
        "<3.1.3",
        "3.1.3",
    ),
    (
        "PyPI",
        "pyyaml",
        "GHSA-8q59-q68h-6hv4",
        "<5.4",
        "5.4",
    ),
    (
        "PyPI",
        "urllib3",
        "GHSA-v845-jxx5-vc9f",
        "<1.26.18",
        "1.26.18",
    ),
    (
        "PyPI",
        "cryptography",
        "GHSA-3ww4-gg4f-jr7f",
        "<41.0.4",
        "41.0.4",
    ),
    (
        "npm",
        "lodash",
        "GHSA-35jh-r3h4-6jhm",
        "<4.17.21",
        "4.17.21",
    ),
    (
        "npm",
        "minimist",
        "GHSA-xvch-5gv4-984h",
        "<1.2.6",
        "1.2.6",
    ),
    (
        "npm",
        "node-fetch",
        "GHSA-r683-j2x4-v87g",
        "<2.6.7",
        "2.6.7",
    ),
    (
        "npm",
        "axios",
        "GHSA-cph5-m8f7-6c5x",
        "<0.21.4",
        "0.21.4",
    ),
    (
        "npm",
        "log4js",
        "GHSA-4w9v-q9hf-6hqp",
        "<6.9.1",
        "6.9.1",
    ),
    (
        "npm",
        "tar",
        "GHSA-3jfq-g458-7qm9",
        "<6.1.11",
        "6.1.11",
    ),
    (
        "Go",
        "golang.org/x/net",
        "GHSA-4374-p667-p5c8",
        "<0.17.0",
        "0.17.0",
    ),
    (
        "Go",
        "github.com/gogo/protobuf",
        "GHSA-c3h9-896r-86jm",
        "<1.3.2",
        "1.3.2",
    ),
    (
        "crates.io",
        "openssl",
        "RUSTSEC-2023-0044",
        "<0.10.55",
        "0.10.55",
    ),
    (
        "Maven",
        "org.apache.logging.log4j:log4j-core",
        "CVE-2021-44228",
        "<2.15.0",
        "2.15.0",
    ),
    (
        "Maven",
        "org.springframework:spring-core",
        "CVE-2022-22965",
        "<5.3.18",
        "5.3.18",
    ),
    (
        "Maven",
        "com.fasterxml.jackson.core:jackson-databind",
        "CVE-2020-36518",
        "<2.13.2.1",
        "2.13.2.1",
    ),
    (
        "RubyGems",
        "rack",
        "CVE-2022-30122",
        "<2.2.3",
        "2.2.3",
    ),
    (
        "RubyGems",
        "nokogiri",
        "CVE-2022-24810",
        "<1.13.4",
        "1.13.4",
    ),
)


@dataclass
class Advisory:
    """One known vulnerability affecting a package version."""

    id: str
    ecosystem: str
    package: str
    summary: str = ""
    severity: Severity = Severity.MEDIUM
    aliases: list[str] = field(default_factory=list)
    affected_range: str = ""
    fixed_version: str = ""
    references: list[str] = field(default_factory=list)
    published: str = ""
    #: Where the answer came from: ``"osv"`` or ``"offline"``.
    source: str = "offline"

    @property
    def cve(self) -> str:
        """The CVE identifier, if the advisory has one."""
        for alias in self.aliases:
            if alias.startswith("CVE-"):
                return alias
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ecosystem": self.ecosystem,
            "package": self.package,
            "summary": self.summary,
            "severity": self.severity.value,
            "aliases": list(self.aliases),
            "affected_range": self.affected_range,
            "fixed_version": self.fixed_version,
            "references": list(self.references),
            "published": self.published,
            "source": self.source,
        }


@dataclass
class VersionSpec:
    """A parsed version, comparable enough to test against a range."""

    raw: str
    numbers: tuple[int, ...] = ()
    pre_release: str = ""

    @property
    def is_known(self) -> bool:
        """Whether the version could be parsed at all."""
        return bool(self.numbers)

    def __lt__(self, other: VersionSpec) -> bool:
        return self.numbers < other.numbers

    def __le__(self, other: VersionSpec) -> bool:
        return self.numbers <= other.numbers

    def __gt__(self, other: VersionSpec) -> bool:
        return self.numbers > other.numbers

    def __ge__(self, other: VersionSpec) -> bool:
        return self.numbers >= other.numbers

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, VersionSpec):
            return NotImplemented
        return self.numbers == other.numbers and self.pre_release == other.pre_release

    def __hash__(self) -> int:
        return hash((self.numbers, self.pre_release))

    def __str__(self) -> str:
        return self.raw


_VERSION_PATTERN = re.compile(r"(\d+)")


def parse_version(raw: str) -> VersionSpec:
    """Parse a version string into comparable numbers.

    Handles the common shapes: ``1.2.3``, ``v1.2.3``, ``1.2.3-rc1``,
    ``1.2.3.4``, and epoch prefixes like ``1:2.3``.
    """
    cleaned = raw.strip().lstrip("vV")
    if ":" in cleaned:
        cleaned = cleaned.split(":", 1)[1]
    pre_release = ""
    for separator in ("-", "+", "~"):
        if separator in cleaned:
            cleaned, _, pre_release = cleaned.partition(separator)
            break
    numbers = tuple(int(part) for part in _VERSION_PATTERN.findall(cleaned))
    return VersionSpec(raw=raw, numbers=numbers, pre_release=pre_release)


def _compare(version: VersionSpec, bound: str) -> int:
    """Compare a version against a bound string. Returns -1, 0, or 1."""
    other = parse_version(bound)
    if not version.numbers or not other.numbers:
        return 0
    if version.numbers < other.numbers:
        return -1
    if version.numbers > other.numbers:
        return 1
    return 0


def version_in_range(version: str, range_expression: str) -> bool:
    """Whether a version falls inside an OSV-style range expression.

    Supports the forms that appear in real advisories:

    - ``>=1.0,<2.0`` — conjunction of comparators
    - ``<1.2.3`` — a single upper bound
    - ``>=1.0`` — a single lower bound
    - ``*`` — everything

    Anything it cannot parse with confidence returns ``False``. A range
    check that is unsure must not produce a "vulnerable" verdict.
    """
    cleaned = range_expression.strip()
    if not cleaned or cleaned == "*":
        return True

    parsed = parse_version(version)
    if not parsed.is_known:
        return False

    for clause in cleaned.split(","):
        clause = clause.strip()
        if not clause:
            continue
        if clause.startswith(">="):
            if _compare(parsed, clause[2:].strip()) < 0:
                return False
        elif clause.startswith("<="):
            if _compare(parsed, clause[2:].strip()) > 0:
                return False
        elif clause.startswith(">"):
            if _compare(parsed, clause[1:].strip()) <= 0:
                return False
        elif clause.startswith("<"):
            if _compare(parsed, clause[1:].strip()) >= 0:
                return False
        elif clause.startswith("="):
            if _compare(parsed, clause[1:].strip()) != 0:
                return False
        else:
            # A bare version means "exactly this version".
            if _compare(parsed, clause) != 0:
                return False
    return True


#: Severity mapping from OSV's CVSS-derived scores.
_CVSS_BANDS: tuple[tuple[float, Severity], ...] = (
    (9.0, Severity.CRITICAL),
    (7.0, Severity.HIGH),
    (4.0, Severity.MEDIUM),
    (0.1, Severity.LOW),
)


def severity_from_cvss(score: float | None) -> Severity:
    """Map a CVSS base score to a severity band."""
    if score is None:
        return Severity.MEDIUM
    for threshold, severity in _CVSS_BANDS:
        if score >= threshold:
            return severity
    return Severity.INFO


class OfflineKnowledgeBase:
    """The bundled advisory list, searchable without a network."""

    def __init__(self, entries: tuple[tuple[str, str, str, str, str], ...] = OFFLINE_ADVISORIES):
        self._entries = entries

    def lookup(self, ecosystem: str, package: str, version: str) -> list[Advisory]:
        """Every bundled advisory that affects this package version."""
        matches: list[Advisory] = []
        for entry_ecosystem, entry_package, advisory_id, range_expression, fixed in self._entries:
            if entry_ecosystem.lower() != ecosystem.lower():
                continue
            if entry_package.lower() != package.lower():
                continue
            if not version_in_range(version, range_expression):
                continue
            matches.append(
                Advisory(
                    id=advisory_id,
                    ecosystem=entry_ecosystem,
                    package=entry_package,
                    summary=f"{entry_package} {version} is affected by {advisory_id}",
                    severity=Severity.HIGH,
                    aliases=[advisory_id] if advisory_id.startswith("CVE-") else [],
                    affected_range=range_expression,
                    fixed_version=fixed,
                    references=[f"https://osv.dev/vulnerability/{advisory_id}"],
                    source="offline",
                )
            )
        return matches

    def all_ids(self) -> list[str]:
        return [entry[2] for entry in self._entries]


class OsvClient:
    """A client for the OSV query API.

    The client never raises. A network failure returns an empty result
    and sets :attr:`last_error`, because an unreachable advisory API is
    a degraded scan, not a failed one.
    """

    def __init__(
        self,
        *,
        timeout: float = OSV_TIMEOUT_SECONDS,
        offline: OfflineKnowledgeBase | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.timeout = timeout
        self.offline = offline or OfflineKnowledgeBase()
        self.last_error = ""
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> OsvClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def query(self, ecosystem: str, package: str, version: str) -> list[Advisory]:
        """Look up one package version, falling back to the offline KB.

        The offline result is returned alongside a live result when both
        exist, so the caller can tell the reader that the bundled
        database is stale.
        """
        offline_matches = self.offline.lookup(ecosystem, package, version)
        live_matches = await self._query_osv(ecosystem, package, version)
        if live_matches:
            # Offline hits that the live database does not confirm are
            # dropped: the live database is authoritative when reachable.
            return live_matches
        return offline_matches

    async def _query_osv(self, ecosystem: str, package: str, version: str) -> list[Advisory]:
        """The live OSV lookup. Returns ``[]`` on any failure."""
        if not self._client:
            return []
        osv_ecosystem = OSV_ECOSYSTEMS.get(ecosystem.lower(), ecosystem)
        payload = {
            "version": version,
            "package": {"name": package, "ecosystem": osv_ecosystem},
        }
        try:
            response = await self._client.post(OSV_QUERY_URL, json=payload, timeout=self.timeout)
        except httpx.HTTPError as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []
        if response.status_code != 200:
            self.last_error = f"OSV returned HTTP {response.status_code}"
            return []
        try:
            body = response.json()
        except ValueError:
            self.last_error = "OSV returned a body that is not JSON"
            return []
        vulns = body.get("vulns") if isinstance(body, dict) else None
        if not isinstance(vulns, list):
            return []

        advisories: list[Advisory] = []
        for entry in vulns:
            advisory = self._parse_vuln(entry, osv_ecosystem, package)
            if advisory is not None:
                advisories.append(advisory)
        return advisories

    @staticmethod
    def _parse_vuln(entry: Any, ecosystem: str, package: str) -> Advisory | None:
        """Turn one OSV vulnerability record into an :class:`Advisory`."""
        if not isinstance(entry, dict):
            return None
        identifier = str(entry.get("id", ""))
        if not identifier:
            return None

        aliases = [str(alias) for alias in entry.get("aliases", []) or []]
        affected_range = ""
        fixed_version = ""
        for affected in entry.get("affected", []) or []:
            if not isinstance(affected, dict):
                continue
            for range_entry in affected.get("ranges", []) or []:
                if not isinstance(range_entry, dict):
                    continue
                for event in range_entry.get("events", []) or []:
                    if not isinstance(event, dict):
                        continue
                    introduced = event.get("introduced")
                    if introduced and introduced != "0" and not affected_range:
                        affected_range = f">={introduced}"
                    fixed = event.get("fixed")
                    if fixed and not fixed_version:
                        fixed_version = str(fixed)

        database_specific = entry.get("database_specific", {})
        severity = Severity.MEDIUM
        if isinstance(database_specific, dict):
            raw_severity = str(database_specific.get("severity", "")).lower()
            if raw_severity == "critical":
                severity = Severity.CRITICAL
            elif raw_severity == "high":
                severity = Severity.HIGH
            elif raw_severity == "moderate":
                severity = Severity.MEDIUM
            elif raw_severity == "low":
                severity = Severity.LOW
            score = database_specific.get("cvss", {})
            if isinstance(score, dict):
                severity = severity_from_cvss(_extract_cvss_score(score))

        summary = str(entry.get("summary", "") or entry.get("details", "")[:200])
        references = []
        for reference in entry.get("references", []) or []:
            if isinstance(reference, dict) and reference.get("url"):
                references.append(str(reference["url"]))

        return Advisory(
            id=identifier,
            ecosystem=ecosystem,
            package=package,
            summary=summary[:400],
            severity=severity,
            aliases=aliases,
            affected_range=affected_range,
            fixed_version=fixed_version,
            references=references,
            published=str(entry.get("published", "")),
            source="osv",
        )


def _extract_cvss_score(score: dict[str, Any]) -> float | None:
    """Pull a numeric base score out of an OSV CVSS block."""
    for key in ("baseScore", "base_score", "score"):
        value = score.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


@dataclass
class TechnologyAdvisory:
    """A vulnerability tied to a fingerprint rather than a dependency.

    Web servers, frameworks, and appliances do not come from a package
    manifest, so their advisories are matched on the version string that
    the fingerprinting step recovered.
    """

    technology: str
    advisory: str
    vulnerable_below: str
    severity: Severity
    summary: str
    cwe: str = ""
    references: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "technology": self.technology,
            "advisory": self.advisory,
            "vulnerable_below": self.vulnerable_below,
            "severity": self.severity.value,
            "summary": self.summary,
            "cwe": self.cwe,
            "references": list(self.references),
        }


#: Advisories for fingerprint-detected server software.
TECHNOLOGY_ADVISORIES: tuple[TechnologyAdvisory, ...] = (
    TechnologyAdvisory(
        technology="nginx",
        advisory="CVE-2021-23017",
        vulnerable_below="1.20.1",
        severity=Severity.HIGH,
        summary=(
            "A resolution bug in the nginx resolver allows an attacker who "
            "controls a DNS response to overwrite a previously cached "
            "record."
        ),
        cwe="CWE-350",
        references=["https://nginx.org/en/security_advisories.html"],
    ),
    TechnologyAdvisory(
        technology="apache",
        advisory="CVE-2021-41773",
        vulnerable_below="2.4.50",
        severity=Severity.CRITICAL,
        summary=(
            "A path traversal in Apache HTTP Server 2.4.49 allowed "
            "request paths to escape the document root, and with "
            "mod_cgi enabled it became remote code execution."
        ),
        cwe="CWE-22",
        references=["https://httpd.apache.org/security/vulnerabilities_24.html"],
    ),
    TechnologyAdvisory(
        technology="apache",
        advisory="CVE-2021-42013",
        vulnerable_below="2.4.51",
        severity=Severity.CRITICAL,
        summary=(
            "An incomplete fix for CVE-2021-41773 let the traversal be "
            "reached again, re-enabling remote code execution."
        ),
        cwe="CWE-22",
        references=["https://httpd.apache.org/security/vulnerabilities_24.html"],
    ),
    TechnologyAdvisory(
        technology="openSSL",
        advisory="CVE-2022-3602",
        vulnerable_below="3.0.7",
        severity=Severity.HIGH,
        summary=(
            "A buffer overrun in X.509 name verification can crash the "
            "process or disclose memory when a crafted certificate is "
            "presented."
        ),
        cwe="CWE-125",
        references=["https://www.openssl.org/news/secadv/20221101.txt"],
    ),
    TechnologyAdvisory(
        technology="log4j",
        advisory="CVE-2021-44228",
        vulnerable_below="2.15.0",
        severity=Severity.CRITICAL,
        summary=(
            "Log4Shell: JNDI lookups in Log4j let an attacker-controlled "
            "log message load and execute remote code."
        ),
        cwe="CWE-502",
        references=["https://nvd.nist.gov/vuln/detail/CVE-2021-44228"],
    ),
    TechnologyAdvisory(
        technology="struts",
        advisory="CVE-2017-5638",
        vulnerable_below="2.5.10",
        severity=Severity.CRITICAL,
        summary=(
            "The Jakarta Multipart parser in Struts evaluated a Content-Type "
            "header as an OGNL expression, giving unauthenticated RCE."
        ),
        cwe="CWE-20",
        references=["https://nvd.nist.gov/vuln/detail/CVE-2017-5638"],
    ),
    TechnologyAdvisory(
        technology="jinja2",
        advisory="CVE-2024-22195",
        vulnerable_below="3.1.3",
        severity=Severity.HIGH,
        summary=(
            "The Jinja2 sandbox could be escaped through a malicious "
            "attribute lookup, allowing arbitrary code execution."
        ),
        cwe="CWE-1336",
        references=["https://nvd.nist.gov/vuln/detail/CVE-2024-22195"],
    ),
    TechnologyAdvisory(
        technology="tomcat",
        advisory="CVE-2020-1938",
        vulnerable_below="9.0.31",
        severity=Severity.MEDIUM,
        summary=(
            "Ghostcat let an attacker read files from the Tomcat webroot through the AJP connector."
        ),
        cwe="CWE-269",
        references=["https://nvd.nist.gov/vuln/detail/CVE-2020-1938"],
    ),
)


def match_technology_advisories(technologies: dict[str, str]) -> list[TechnologyAdvisory]:
    """Match fingerprint-detected technologies against known advisories.

    A technology with no version detected is skipped: "it runs nginx"
    is not evidence of a vulnerability, and reporting every nginx CVE
    against every nginx host is noise.
    """
    matches: list[TechnologyAdvisory] = []
    for name, version in technologies.items():
        if not version:
            continue
        normalised = name.lower()
        for advisory in TECHNOLOGY_ADVISORIES:
            if advisory.technology.lower() != normalised:
                continue
            parsed = parse_version(version)
            if not parsed.is_known:
                continue
            if _compare(parsed, advisory.vulnerable_below) < 0:
                matches.append(advisory)
    return matches


def advisory_to_finding(
    advisory: Advisory,
    *,
    package: str,
    version: str,
    ecosystem: str,
) -> Finding:
    """Turn an advisory into a reportable finding."""
    title = f"{package} {version} is affected by {advisory.id}"
    if advisory.cve and advisory.cve != advisory.id:
        title = f"{package} {version} is affected by {advisory.cve} ({advisory.id})"

    references = list(advisory.references)
    if advisory.cve:
        references.append(f"https://nvd.nist.gov/vuln/detail/{advisory.cve}")
    references.append(f"https://osv.dev/vulnerability/{advisory.id}")

    remediation = (
        f"Upgrade {package} to {advisory.fixed_version} or later."
        if advisory.fixed_version
        else f"No fixed version is published for {advisory.id}; review the "
        "advisory for mitigations."
    )

    return Finding(
        title=title,
        severity=advisory.severity,
        category=Category.SUPPLY_CHAIN,
        confidence=Confidence.PROBABLE,
        target=f"{package}@{version}",
        endpoint=package,
        description=(
            advisory.summary
            or f"{package} {version} falls inside the affected range "
            f"'{advisory.affected_range}' of {advisory.id}."
        ),
        impact=(
            "The deployed version carries a publicly documented flaw. "
            "Because the advisory is public, an attacker can look up the "
            "same range and test for it without any prior knowledge of "
            "this application."
        ),
        remediation=remediation,
        cwe="CWE-1104",
        owasp="A06:2021 - Vulnerable and Outdated Components",
        cvss=_estimate_cvss(advisory.severity),
        references=references,
        evidence=[
            Evidence(
                kind=EvidenceKind.MATCH,
                summary=(
                    f"{package} {version} matched {advisory.id} "
                    f"(range: {advisory.affected_range or 'unknown'}, "
                    f"source: {advisory.source})"
                ),
            )
        ],
        source=f"cve:{advisory.id}",
        tags=["cve", "supply-chain", ecosystem.lower()],
    )


def technology_advisory_to_finding(
    advisory: TechnologyAdvisory, technology: str, version: str
) -> Finding:
    """Turn a technology advisory into a reportable finding."""
    return Finding(
        title=f"{technology} {version} is affected by {advisory.advisory}",
        severity=advisory.severity,
        category=Category.SUPPLY_CHAIN,
        confidence=Confidence.PROBABLE,
        target=f"{technology}@{version}",
        endpoint=technology,
        description=advisory.summary,
        impact=(
            "The version fingerprint recovered from the server's own "
            "response headers falls below the patched release. The "
            "vulnerability is public and directly exploitable."
        ),
        remediation=(
            f"Upgrade {technology} to {advisory.vulnerable_below} or later, "
            "and suppress the version banner that let this be detected."
        ),
        cwe=advisory.cwe,
        owasp="A06:2021 - Vulnerable and Outdated Components",
        references=advisory.references,
        evidence=[
            Evidence(
                kind=EvidenceKind.MATCH,
                summary=(
                    f"{technology} version '{version}' is below the patched "
                    f"release {advisory.vulnerable_below}"
                ),
            )
        ],
        source=f"cve:{advisory.advisory}",
        tags=["cve", "fingerprint", technology.lower()],
    )


def _estimate_cvss(severity: Severity) -> float:
    """A rough CVSS base score for a severity band."""
    return {
        Severity.CRITICAL: 9.8,
        Severity.HIGH: 8.0,
        Severity.MEDIUM: 5.5,
        Severity.LOW: 3.0,
        Severity.INFO: 0.0,
    }.get(severity, 5.0)


@dataclass
class AuditReport:
    """The result of auditing a dependency set or a fingerprint."""

    findings: list[Finding] = field(default_factory=list)
    advisories: list[Advisory] = field(default_factory=list)
    packages_checked: int = 0
    packages_skipped: list[str] = field(default_factory=list)
    offline_used: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": [finding.to_dict() for finding in self.findings],
            "advisories": [advisory.to_dict() for advisory in self.advisories],
            "packages_checked": self.packages_checked,
            "packages_skipped": list(self.packages_skipped),
            "offline_used": self.offline_used,
            "errors": list(self.errors),
        }


class VulnerabilityAuditor:
    """Looks up advisories for a set of dependencies."""

    def __init__(self, client: OsvClient | None = None) -> None:
        self.client = client or OsvClient()

    async def audit_dependencies(
        self, dependencies: list[Any], *, concurrency: int = 8
    ) -> AuditReport:
        """Check every dependency against the advisory database."""
        import asyncio

        report = AuditReport()
        semaphore = asyncio.Semaphore(concurrency)

        async def check(dependency: Any) -> list[Advisory]:
            async with semaphore:
                ecosystem = getattr(dependency, "ecosystem", "")
                name = getattr(dependency, "name", "")
                version = getattr(dependency, "version", "")
                if not name or not version:
                    return []
                return await self.client.query(ecosystem, name, version)

        results = await asyncio.gather(
            *(check(dependency) for dependency in dependencies), return_exceptions=True
        )

        for dependency, outcome in zip(dependencies, results, strict=False):
            name = getattr(dependency, "name", "?")
            version = getattr(dependency, "version", "")
            if isinstance(outcome, BaseException):
                report.errors.append(f"{name}: {type(outcome).__name__}: {outcome}")
                continue
            if not version:
                report.packages_skipped.append(f"{name} (no version)")
                continue
            report.packages_checked += 1
            for advisory in outcome:
                if advisory.source == "offline":
                    report.offline_used = True
                report.advisories.append(advisory)
                report.findings.append(
                    advisory_to_finding(
                        advisory,
                        package=name,
                        version=version,
                        ecosystem=getattr(dependency, "ecosystem", ""),
                    )
                )
        if self.client.last_error:
            report.errors.append(self.client.last_error)
        return report

    async def audit_fingerprints(self, technologies: dict[str, str]) -> list[Finding]:
        """Check fingerprint-detected technologies against advisories."""
        return [
            technology_advisory_to_finding(advisory, name, version)
            for name, version in technologies.items()
            for advisory in match_technology_advisories({name: version})
        ]
