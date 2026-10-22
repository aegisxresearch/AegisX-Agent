"""The swarm — several specialists attacking one target at once.

A single scanner walks a single path: it maps, then it checks, then it
reports. Real assessments are not like that. A serious weakness is
usually found by two different techniques meeting — a directory
listing that reveals a backup file, and a fingerprint that says which
framework turns that backup file into a path traversal.

The swarm runs several *lines of attack* concurrently, each with its
own brief, its own tool subset, and its own findings, then merges the
results into one store. Three properties make this safe and useful:

- **Scoped tool subsets.** A line of attack can only use the tools it
  was given. The recon line cannot send injection payloads, and the
  injection line cannot scan ports. A mistake in one agent's reasoning
  cannot become a request the operator never authorised, because the
  capability was never handed to it.
- **A shared, deduplicated store.** Every line writes into the same
  :class:`~aegisx_agent.cyber.store.FindingStore`, so two lines that
  find the same problem produce one finding with merged evidence.
- **Consensus.** When two lines independently report the same
  fingerprint, the confidence is raised. A finding that only one line
  saw stays where it is.

The swarm runs *deterministically* when given a fixed set of lines.
The LLM-driven variant lives in :mod:`aegisx_agent.cyber.agent`, which
uses the swarm as its execution substrate.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from aegisx_agent.cyber.checks import CheckContext, run_checks
from aegisx_agent.cyber.models import Category, Confidence, Finding, ScanStats
from aegisx_agent.cyber.prober import Prober
from aegisx_agent.cyber.recon import Crawler, fingerprint
from aegisx_agent.cyber.scope import ScopeGuard
from aegisx_agent.cyber.store import FindingStore

#: The capability names a line of attack can hold.
CAPABILITIES: frozenset[str] = frozenset(
    {
        "crawl",
        "fingerprint",
        "port-scan",
        "subdomain-enum",
        "inject",
        "ssrf",
        "xxe",
        "command-injection",
        "nosql",
        "authz",
        "token",
        "protocol",
        "api-surface",
        "dns",
        "cloud",
        "exposure",
        "headers",
        "tls",
        "cors",
        "secrets",
        "static-analysis",
    }
)


@dataclass(frozen=True)
class ToolGrant:
    """The capabilities one line of attack holds.

    A grant is a capability list, not a function list. A line can
    perform any action the capability permits, but it cannot perform
    an action no capability covers. This is the difference between
    "the agent may scan ports" and "the agent may call this specific
    function", and the former is what actually constrains behaviour.
    """

    capabilities: frozenset[str]

    def permits(self, capability: str) -> bool:
        """Whether this grant covers a capability."""
        return capability in self.capabilities

    def require(self, capability: str) -> None:
        """Raise when a capability is not granted."""
        if not self.permits(capability):
            raise PermissionError(
                f"capability {capability!r} is not granted; granted: {sorted(self.capabilities)}"
            )


@dataclass
class LineOfAttack:
    """One specialist's brief."""

    name: str
    brief: str
    grant: ToolGrant
    #: Categories this line is expected to produce. Used to route
    #: findings and to decide whether the line contributed anything.
    categories: tuple[Category, ...] = ()
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "brief": self.brief,
            "capabilities": sorted(self.grant.capabilities),
            "categories": [category.value for category in self.categories],
            "findings": len(self.findings),
            "notes": list(self.notes),
            "error": self.error,
        }


@dataclass
class SwarmPlan:
    """A set of lines of attack and the target they will run against."""

    target: str
    base_url: str
    lines: list[LineOfAttack] = field(default_factory=list)
    max_concurrency: int = 4

    def add_line(self, line: LineOfAttack) -> None:
        self.lines.append(line)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "base_url": self.base_url,
            "max_concurrency": self.max_concurrency,
            "lines": [line.to_dict() for line in self.lines],
        }


@dataclass
class SwarmResult:
    """The merged outcome of a swarm run."""

    findings: list[Finding] = field(default_factory=list)
    lines: list[LineOfAttack] = field(default_factory=list)
    stats: ScanStats = field(default_factory=ScanStats)
    #: Findings that at least two lines independently reported.
    corroborated: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": [finding.to_dict() for finding in self.findings],
            "lines": [line.to_dict() for line in self.lines],
            "stats": self.stats.to_dict(),
            "corroborated": list(self.corroborated),
        }


def default_lines(base_url: str) -> list[LineOfAttack]:
    """The standard set of lines for a web application target.

    Each line is deliberately narrow. The overlap between them is
    what produces the corroboration signal.
    """
    return [
        LineOfAttack(
            name="surface-mapper",
            brief=(
                "Map the attack surface: crawl the site, read robots.txt and "
                "the sitemap, and fingerprint the technologies in use. Report "
                "every endpoint you find and anything that is exposed."
            ),
            grant=ToolGrant(frozenset({"crawl", "fingerprint"})),
            categories=(Category.RECON, Category.EXPOSURE),
        ),
        LineOfAttack(
            name="transport-auditor",
            brief=(
                "Audit the transport and browser-facing configuration: TLS, "
                "HSTS, security headers, cookie attributes, and CORS policy."
            ),
            grant=ToolGrant(frozenset({"headers", "tls", "cors"})),
            categories=(Category.HEADERS, Category.COOKIE, Category.CORS, Category.TRANSPORT),
        ),
        LineOfAttack(
            name="injection-hunter",
            brief=(
                "Hunt for injection: SQL injection, reflected XSS, template "
                "injection, and path traversal, using differential techniques "
                "that prove the flaw rather than assume it."
            ),
            grant=ToolGrant(frozenset({"inject"})),
            categories=(Category.INJECTION,),
        ),
        LineOfAttack(
            name="access-control-auditor",
            brief=(
                "Audit access control: look for object identifiers that are "
                "not bound to the caller, dangerous HTTP methods, and open "
                "redirects that enable phishing."
            ),
            grant=ToolGrant(frozenset({"authz"})),
            categories=(Category.AUTHZ, Category.CONFIG),
        ),
        LineOfAttack(
            name="server-side-hunter",
            brief=(
                "Hunt for server-side injection: SSRF through URL parameters, "
                "XML external entities, operating-system command injection, and "
                "NoSQL operator injection. Every probe is read-only and proves "
                "the flaw by differential response."
            ),
            grant=ToolGrant(frozenset({"ssrf", "xxe", "command-injection", "nosql"})),
            categories=(Category.INJECTION,),
        ),
        LineOfAttack(
            name="identity-auditor",
            brief=(
                "Audit the tokens the service issues: unsigned JWTs, tokens "
                "that never expire, authorisation decisions carried in the "
                "client, credentials passed in URLs, and low-entropy session "
                "identifiers."
            ),
            grant=ToolGrant(frozenset({"token"})),
            categories=(Category.AUTHZ, Category.CONFIG),
        ),
        LineOfAttack(
            name="surface-auditor",
            brief=(
                "Audit the exposed surface: administration and debug paths, "
                "GraphQL schemas, HTTP response splitting, and host header "
                "injection."
            ),
            grant=ToolGrant(frozenset({"api-surface", "protocol"})),
            categories=(Category.EXPOSURE, Category.INJECTION),
        ),
    ]


class SwarmRunner:
    """Executes a swarm plan against one target."""

    def __init__(
        self,
        plan: SwarmPlan,
        *,
        guard: ScopeGuard,
        store: FindingStore | None = None,
    ) -> None:
        self.plan = plan
        self.guard = guard
        self.store = store or FindingStore()
        self._surface_endpoints: list[str] = []
        self._technologies: dict[str, str] = {}

    async def run(self) -> SwarmResult:
        """Run every line concurrently and merge the results."""
        result = SwarmResult()
        semaphore = asyncio.Semaphore(self.plan.max_concurrency)

        async with Prober(self.guard) as prober:
            # The surface mapper runs first and alone: the other lines
            # need its endpoints to have anything to attack.
            mapper = next(
                (line for line in self.plan.lines if line.grant.permits("crawl")),
                None,
            )
            if mapper is not None:
                await self._run_mapper(prober, mapper)
                result.lines.append(mapper)

            attack_lines = [line for line in self.plan.lines if line is not mapper]
            tasks = [self._run_line(prober, line, semaphore) for line in attack_lines]
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)

            for line, outcome in zip(attack_lines, outcomes, strict=False):
                if isinstance(outcome, BaseException):
                    line.error = f"{type(outcome).__name__}: {outcome}"
                result.lines.append(line)

        result.findings = self.store.all()
        self._corroborate(result)
        result.stats.findings_total = len(result.findings)
        histogram: dict[str, int] = {}
        for finding in result.findings:
            histogram[finding.severity.value] = histogram.get(finding.severity.value, 0) + 1
        result.stats.findings_by_severity = histogram
        result.stats.requests_sent = self.guard.requests_made
        return result

    async def _run_mapper(self, prober: Prober, line: LineOfAttack) -> None:
        """Run the surface-mapping line and publish what it found."""
        try:
            line.grant.require("crawl")
            crawler = Crawler(prober, self.guard, max_pages=200, max_depth=3)
            surface = await crawler.crawl(self.plan.base_url)
            self._surface_endpoints = list(surface.endpoints)
            if line.grant.permits("fingerprint"):
                response = await prober.request(self.plan.base_url)
                if not response.error:
                    self._technologies = fingerprint(response)
            line.notes.append(f"mapped {len(self._surface_endpoints)} endpoints")
            line.notes.append(f"technologies: {', '.join(self._technologies) or 'none'}")
        except PermissionError as exc:
            line.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - one line must not end the swarm
            line.error = f"{type(exc).__name__}: {exc}"

    async def _run_line(
        self, prober: Prober, line: LineOfAttack, semaphore: asyncio.Semaphore
    ) -> None:
        """Run one attack line within its capability grant."""
        async with semaphore:
            try:
                produced = await self._execute(prober, line)
                line.findings.extend(produced)
                self.store.extend(produced)
            except PermissionError as exc:
                line.error = str(exc)
            except Exception as exc:  # noqa: BLE001 - one line must not end the swarm
                line.error = f"{type(exc).__name__}: {exc}"

    async def _execute(self, prober: Prober, line: LineOfAttack) -> list[Finding]:
        """Dispatch a line to the checks its grant permits."""
        from aegisx_agent.cyber.checks.api import AdminSurfaceCheck, GraphQLCheck
        from aegisx_agent.cyber.checks.authn import TokenCheck
        from aegisx_agent.cyber.checks.authz import (
            HTTPMethodCheck,
            IDORCheck,
            OpenRedirectCheck,
            TransportCheck,
        )
        from aegisx_agent.cyber.checks.dns import CloudStorageCheck, SubdomainTakeoverCheck
        from aegisx_agent.cyber.checks.exposure import (
            CORSCheck,
            DirectoryListingCheck,
            SensitiveFileCheck,
            VerboseErrorCheck,
        )
        from aegisx_agent.cyber.checks.headers import HeaderCheck
        from aegisx_agent.cyber.checks.injection import (
            CommandInjectionCheck,
            NoSQLInjectionCheck,
            PathTraversalCheck,
            SQLInjectionCheck,
            TemplateInjectionCheck,
            XSSCheck,
            XXECheck,
        )
        from aegisx_agent.cyber.checks.protocol import CRLFInjectionCheck, HostHeaderCheck
        from aegisx_agent.cyber.checks.ssrf import SSRFCheck

        checks: list[Any] = []
        if line.grant.permits("headers"):
            checks.append(HeaderCheck())
        if line.grant.permits("tls"):
            checks.append(TransportCheck())
        if line.grant.permits("cors"):
            checks.append(CORSCheck())
        if line.grant.permits("exposure"):
            checks.extend([SensitiveFileCheck(), DirectoryListingCheck(), VerboseErrorCheck()])
        if line.grant.permits("inject"):
            checks.extend(
                [SQLInjectionCheck(), XSSCheck(), TemplateInjectionCheck(), PathTraversalCheck()]
            )
        if line.grant.permits("ssrf"):
            checks.append(SSRFCheck())
        if line.grant.permits("xxe"):
            checks.append(XXECheck())
        if line.grant.permits("command-injection"):
            checks.append(CommandInjectionCheck())
        if line.grant.permits("nosql"):
            checks.append(NoSQLInjectionCheck())
        if line.grant.permits("token"):
            checks.append(TokenCheck())
        if line.grant.permits("protocol"):
            checks.extend([CRLFInjectionCheck(), HostHeaderCheck()])
        if line.grant.permits("api-surface"):
            checks.extend([AdminSurfaceCheck(), GraphQLCheck()])
        if line.grant.permits("dns"):
            checks.append(SubdomainTakeoverCheck())
        if line.grant.permits("cloud"):
            checks.append(CloudStorageCheck())
        if line.grant.permits("authz"):
            checks.extend([IDORCheck(), HTTPMethodCheck(), OpenRedirectCheck()])

        if not checks:
            return []

        context = CheckContext(
            base_url=self.plan.base_url,
            host=self.guard.policy.allowed_hosts[0] if self.guard.policy.allowed_hosts else "",
            prober=prober,
            endpoints=list(self._surface_endpoints),
        )
        return await run_checks(checks, context)

    def _corroborate(self, result: SwarmResult) -> None:
        """Raise the confidence of findings that two lines agreed on."""
        reported_by: dict[str, set[str]] = {}
        for line in result.lines:
            for finding in line.findings:
                reported_by.setdefault(finding.fingerprint, set()).add(line.name)

        for fingerprint_key, line_names in reported_by.items():
            if len(line_names) < 2:
                continue
            result.corroborated.append(fingerprint_key)
            corroborated = self.store.findings.get(fingerprint_key)
            if corroborated is None:
                continue
            if corroborated.confidence is not Confidence.CONFIRMED:
                corroborated.confidence = Confidence.CONFIRMED
                corroborated.evidence.append(_corroboration_evidence(sorted(line_names)))


def _corroboration_evidence(line_names: list[str]) -> Any:
    """Evidence recording that two lines agreed."""
    from aegisx_agent.cyber.models import Evidence, EvidenceKind

    return Evidence(
        kind=EvidenceKind.MATCH,
        summary=(
            f"Corroborated independently by {len(line_names)} lines of attack: "
            f"{', '.join(line_names)}"
        ),
    )


class SwarmPlanner:
    """Builds a swarm plan for a target.

    The planner is the part that would normally be driven by an LLM.
    It is kept as an explicit, inspectable object so that a plan can be
    printed, reviewed, and edited before it runs — an autonomous agent
    that shows its plan is one an operator can trust with a production
    hostname.
    """

    def __init__(self, base_url: str, *, max_concurrency: int = 4) -> None:
        self.base_url = base_url
        self.max_concurrency = max_concurrency

    def plan(self, target: str) -> SwarmPlan:
        """The standard plan for a web target."""
        swarm_plan = SwarmPlan(
            target=target,
            base_url=self.base_url,
            max_concurrency=self.max_concurrency,
        )
        for line in default_lines(self.base_url):
            swarm_plan.add_line(line)
        return swarm_plan
