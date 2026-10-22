"""The scan orchestrator — the pipeline that turns a target into a report.

The order of operations is the whole design:

1. **Scope first.** The policy is built and validated before a single
   packet is sent. An out-of-scope request is refused at the network
   boundary, not by hoping the checks behave.
2. **Recon second.** The surface is mapped before it is attacked, so
   the checks are aimed at real endpoints instead of guesses.
3. **Checks third.** Every check runs against the mapped surface, with
   the cheap ones first and the expensive ones gated behind a decision
   about whether the target looks worth them.
4. **Report last.** The findings, the coverage, and the errors all
   travel together, because a report that omits what was not tested is
   worse than no report.

The orchestrator is deterministic and has no LLM in the loop. The
autonomous *agent* built on top of it (:mod:`aegisx_agent.cyber.agent`)
is the part that reasons; the scanner itself must be reproducible, or
a finding cannot be re-verified.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from aegisx_agent.cyber import recon
from aegisx_agent.cyber.checks import Check, CheckContext, run_checks
from aegisx_agent.cyber.checks.authz import (
    HTTPMethodCheck,
    IDORCheck,
    OpenRedirectCheck,
    TransportCheck,
)
from aegisx_agent.cyber.checks.exposure import (
    CORSCheck,
    DirectoryListingCheck,
    SensitiveFileCheck,
    VerboseErrorCheck,
)
from aegisx_agent.cyber.checks.headers import CookieCheck, HeaderCheck
from aegisx_agent.cyber.checks.injection import (
    PathTraversalCheck,
    SQLInjectionCheck,
    TemplateInjectionCheck,
    XSSCheck,
)
from aegisx_agent.cyber.models import (
    Finding,
    HostInfo,
    ScanResult,
    Severity,
)
from aegisx_agent.cyber.prober import Prober
from aegisx_agent.cyber.scope import ScopeGuard, ScopePolicy, scope_from_targets
from aegisx_agent.cyber.store import FindingStore

#: The cheap checks — always run, on every target.
CHEAP_CHECKS: tuple[Check, ...] = (
    HeaderCheck(),
    CookieCheck(),
    HTTPMethodCheck(),
    OpenRedirectCheck(),
    TransportCheck(),
    SensitiveFileCheck(),
    DirectoryListingCheck(),
    VerboseErrorCheck(),
    CORSCheck(),
)

#: The expensive checks — only run when the target earns them.
DEEP_CHECKS: tuple[Check, ...] = (
    SQLInjectionCheck(),
    XSSCheck(),
    TemplateInjectionCheck(),
    PathTraversalCheck(),
    IDORCheck(),
)


@dataclass
class ScanConfig:
    """Everything the operator can tune about a scan."""

    targets: list[str] = field(default_factory=list)
    #: Extra hosts inside the scope, e.g. a staging subdomain.
    extra_hosts: list[str] = field(default_factory=list)
    #: Restrict the scan to these path prefixes.
    allowed_paths: list[str] = field(default_factory=list)
    max_requests: int = 2000
    max_requests_per_host: int = 400
    requests_per_second: float = 10.0
    max_concurrency: int = 6
    timeout_seconds: float = 1800.0
    allow_private_addresses: bool = False
    verify_tls: bool = False
    max_crawl_pages: int = 150
    max_crawl_depth: int = 3
    #: Run the expensive injection checks.
    deep: bool = True
    #: Discover subdomains over DNS.
    enumerate_subdomains: bool = False
    #: Scan TCP ports.
    scan_ports: bool = False
    #: Extra ports the operator authorised.
    allowed_ports: list[int] = field(default_factory=list)
    user_agent: str = "AegisX-Cyber/1.0 (+authorized-security-testing)"
    follow_redirects: bool = True

    def to_policy(self) -> ScopePolicy:
        """Build the scope policy this configuration describes."""
        policy = scope_from_targets(
            self.targets,
            extra_hosts=self.extra_hosts,
            allowed_paths=self.allowed_paths,
            max_requests=self.max_requests,
            max_requests_per_host=self.max_requests_per_host,
            requests_per_second=self.requests_per_second,
            max_concurrency=self.max_concurrency,
            timeout_seconds=self.timeout_seconds,
            allow_private_addresses=self.allow_private_addresses,
            user_agent=self.user_agent,
            follow_redirects=self.follow_redirects,
        )
        if self.allowed_ports:
            policy.allowed_ports = sorted(set(policy.allowed_ports) | set(self.allowed_ports))
        return policy


@dataclass
class ScanProgress:
    """Progress events emitted during a scan."""

    phase: str
    message: str
    findings_so_far: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "message": self.message,
            "findings_so_far": self.findings_so_far,
        }


class Scanner:
    """Runs a complete assessment of one or more targets."""

    def __init__(
        self,
        config: ScanConfig,
        *,
        guard: ScopeGuard | None = None,
        store: FindingStore | None = None,
    ) -> None:
        self.config = config
        self.policy = config.to_policy()
        self.guard = guard or ScopeGuard(self.policy)
        self.store = store or FindingStore()
        self._on_progress: list[Any] = []

    def on_progress(self, callback: Any) -> None:
        """Register a progress callback."""
        self._on_progress.append(callback)

    def _emit(self, phase: str, message: str) -> None:
        """Notify the progress callbacks."""
        event = ScanProgress(phase=phase, message=message, findings_so_far=len(self.store.findings))
        for callback in self._on_progress:
            try:
                callback(event)
            except Exception:  # noqa: BLE001 - a bad listener must not stop the scan
                continue

    async def run(self) -> ScanResult:
        """Run the full pipeline and return the result."""
        started = time.monotonic()
        result = ScanResult(
            target=", ".join(self.config.targets),
            scope=self._scope_description(),
        )

        if not self.config.targets:
            result.stats.errors.append("no targets were supplied")
            result.finalize()
            return result

        async with Prober(
            self.guard, policy=self.policy, verify_tls=self.config.verify_tls
        ) as prober:
            surfaces: list[recon.Surface] = []
            for target in self.config.targets:
                surface = await self._recon_target(prober, target, result)
                surfaces.append(surface)
                self._emit("recon", f"Mapped {len(surface.endpoints)} endpoints on {surface.host}")

            for surface in surfaces:
                findings = await self._run_checks(prober, surface, result)
                self.store.extend(findings)
                self._emit("checks", f"Checked {surface.host}: {len(findings)} findings")

            # Deduplicate, then detect anything that came back since the
            # last run.
            for fingerprint in self.store.detect_regressions():
                self._emit("regression", f"Regressed: {fingerprint}")

        result.findings = self.store.all()
        result.stats.requests_sent = self.guard.requests_made
        result.stats.duration_seconds = round(time.monotonic() - started, 2)
        result.finalize()
        self._emit("done", result.stats.findings_by_severity.__str__())
        return result

    def _scope_description(self) -> str:
        """A human-readable description of what was authorised."""
        hosts = ", ".join(self.policy.allowed_hosts)
        if self.policy.allowed_paths:
            return f"{hosts} (paths: {', '.join(self.policy.allowed_paths)})"
        return hosts

    async def _recon_target(self, prober: Prober, target: str, result: ScanResult) -> recon.Surface:
        """Map one target's surface."""
        normalised = target if "://" in target else f"https://{target}"
        parsed = urlparse(normalised)
        host = (parsed.hostname or "").lower()

        self._emit("recon", f"Mapping {normalised}")

        base_response = await prober.request(normalised, method="GET")
        technologies = recon.fingerprint(base_response) if not base_response.error else {}

        crawler = recon.Crawler(
            prober,
            self.guard,
            max_pages=self.config.max_crawl_pages,
            max_depth=self.config.max_crawl_depth,
        )
        surface = await crawler.crawl(normalised)
        surface.technologies = technologies
        surface.base_url = normalised

        rules, sitemaps = await recon.fetch_robots(prober, normalised)
        surface.robots_rules = rules
        surface.sitemap_urls = sitemaps

        for sitemap in sitemaps[:3]:
            if self.guard.check(sitemap).allowed:
                surface.sitemap_urls.extend(await recon.fetch_sitemap(prober, sitemap))

        host_info = HostInfo(
            host=host,
            scheme=parsed.scheme or "https",
            port=parsed.port or (443 if parsed.scheme == "https" else 80),
            open=not base_response.error,
            status=base_response.status or None,
            server=base_response.header("server", ""),
            title=self._extract_title(base_response.body),
            technologies=sorted(technologies),
            headers=dict(base_response.headers),
        )
        result.hosts.append(host_info)
        result.stats.hosts_discovered += 1
        result.stats.hosts_scanned += 1

        if self.config.enumerate_subdomains and host:
            enumerator = recon.SubdomainEnumerator(self.guard)
            in_scope, out_of_scope = await enumerator.enumerate(host)
            surface.subdomains.extend(in_scope)
            for subdomain in in_scope:
                result.stats.hosts_discovered += 1

        if self.config.scan_ports and host:
            scanner = recon.PortScanner(allowed_ports=self.policy.allowed_ports)
            port_result = await scanner.scan(host)
            surface.open_ports.extend(port_result.open_ports)
            result.stats.errors.extend(port_result.errors[:5])

        # The recon observations are findings in their own right.
        self.store.extend(
            recon.build_recon_findings(
                surface,
                out_of_scope_hosts=[
                    host for host in (await self._out_of_scope_subdomains(surface.host))
                ],
                open_ports=surface.open_ports,
            )
        )

        result.endpoints.extend(surface.endpoints)
        result.stats.endpoints_crawled = len(result.endpoints)
        return surface

    async def _out_of_scope_subdomains(self, host: str) -> list[str]:
        """Subdomains that resolved but are outside the authorised scope."""
        if not self.config.enumerate_subdomains or not host:
            return []
        enumerator = recon.SubdomainEnumerator(self.guard)
        _, out_of_scope = await enumerator.enumerate(host)
        return out_of_scope

    async def _run_checks(
        self, prober: Prober, surface: recon.Surface, result: ScanResult
    ) -> list[Finding]:
        """Run the check suite against one mapped surface."""
        context = CheckContext(
            base_url=surface.base_url,
            host=surface.host,
            prober=prober,
            endpoints=list(surface.endpoints),
            options={"surface": surface},
        )

        checks: list[Check] = list(CHEAP_CHECKS)
        if self.config.deep and self._deserves_deep_checks(surface):
            checks.extend(DEEP_CHECKS)

        result.stats.checks_run += len(checks)
        findings = await run_checks(checks, context)
        result.stats.errors.extend(context.options.get("errors", []))
        return findings

    def _deserves_deep_checks(self, surface: recon.Surface) -> bool:
        """Whether a target is worth the expensive checks.

        Injection probing against a target with no query parameters
        anywhere is a waste of the budget. The signal is simple: does
        the surface have any parameterised endpoint at all?
        """
        for endpoint in surface.endpoints:
            if urlparse(endpoint).query:
                return True
        return False

    @staticmethod
    def _extract_title(body: str) -> str:
        """The ``<title>`` of an HTML page, if it has one."""
        import re

        match = re.search(r"<title[^>]*>(.*?)</title>", body[:20_000], re.IGNORECASE | re.DOTALL)
        if not match:
            return ""
        return " ".join(match.group(1).split())[:200]


async def scan_target(
    target: str,
    *,
    deep: bool = True,
    max_requests: int = 2000,
    on_progress: Any = None,
) -> ScanResult:
    """Scan a single target — the convenience entry point."""
    config = ScanConfig(targets=[target], deep=deep, max_requests=max_requests)
    scanner = Scanner(config)
    if on_progress is not None:
        scanner.on_progress(on_progress)
    return await scanner.run()


def summarise(result: ScanResult) -> str:
    """A short human summary of a scan result."""
    histogram = result.stats.findings_by_severity
    parts = [
        f"{histogram.get(level, 0)} {level}"
        for level in ("critical", "high", "medium", "low", "info")
        if histogram.get(level, 0)
    ]
    detail = ", ".join(parts) if parts else "no findings"
    return (
        f"{result.target}: {len(result.findings)} findings ({detail}) "
        f"from {result.stats.requests_sent} requests in {result.stats.duration_seconds}s"
    )


def filter_findings(
    findings: list[Finding],
    *,
    minimum_severity: Severity = Severity.INFO,
    categories: list[str] | None = None,
) -> list[Finding]:
    """Filter findings by severity and category."""
    selected = [
        finding for finding in findings if finding.severity.weight >= minimum_severity.weight
    ]
    if categories:
        wanted = {category.lower() for category in categories}
        selected = [finding for finding in selected if finding.category.value in wanted]
    return selected
