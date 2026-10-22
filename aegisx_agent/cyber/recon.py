"""Reconnaissance — mapping the attack surface before attacking it.

A scanner that starts by throwing payloads at ``/`` misses everything
that is not linked from the home page. Real applications hide their
interesting surface in places a crawler has to be told to look:
conditional request handlers, API prefixes listed in a sitemap,
staging hosts, alternate ports, and pages that only exist behind a
path prefix.

This module builds the map that the check suite then works against:

- :class:`Crawler` — a bounded, same-origin crawler that records every
  URL it reaches, including the ones linked from JavaScript and forms.
- :func:`fetch_robots` / :func:`fetch_sitemap` — the two files every
  site publishes on purpose, which are often an honest map of the
  surface that ``robots.txt`` alone does not give.
- :class:`SubdomainEnumerator` — passive wordlist expansion over
  DNS, respecting the scope guard (a resolved name that is out of
  scope is *reported* but never *scanned*).
- :class:`PortScanner` — a TCP connect scan against the ports the
  operator authorised.
- :func:`fingerprint` — technology detection from headers, cookies,
  and body markers, feeding the CVE lookup with version evidence.

Everything here is bounded. The crawler has a page budget, the
enumerators have candidate budgets, and every request still passes
through the scope guard.
"""

from __future__ import annotations

import asyncio
import re
import socket
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

from aegisx_agent.cyber.models import (
    Category,
    Confidence,
    Evidence,
    EvidenceKind,
    Finding,
    Severity,
)
from aegisx_agent.cyber.scope import ScopeGuard

if TYPE_CHECKING:  # avoids a runtime import cycle
    from aegisx_agent.cyber.prober import Prober, Response


#: Paths that are never worth crawling — they are not content, and
#: fetching them is either noise or a self-inflicted denial of service.
EXCLUDED_PATH_MARKERS: tuple[str, ...] = (
    "/logout",
    "/signout",
    "/unsubscribe",
    "/delete",
    "/payment",
    "/checkout",
    "/cart",
    "/admin/delete",
)

#: Extensions that are assets, not pages. A crawler that downloads
#: every stylesheet is a crawler that finds nothing and costs a fortune.
ASSET_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".css",
        ".js",
        ".mjs",
        ".map",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".webp",
        ".ico",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".pdf",
        ".zip",
        ".gz",
        ".tar",
        ".mp4",
        ".mp3",
        ".avi",
        ".mov",
        ".webm",
    }
)

#: Link-bearing HTML attributes, beyond ``href``/``src``.
LINK_ATTRIBUTES: tuple[str, ...] = ("action", "formaction", "data-url", "data-href", "data-src")

#: Common subdomain words. Deliberately short: a long list multiplied
#: by a wildcard DNS provider produces mostly NXDOMAIN noise.
DEFAULT_SUBDOMAIN_WORDS: tuple[str, ...] = (
    "api",
    "app",
    "admin",
    "auth",
    "beta",
    "cdn",
    "dev",
    "demo",
    "docs",
    "internal",
    "mail",
    "portal",
    "sandbox",
    "staging",
    "static",
    "test",
)

#: Ports worth probing on an application host, beyond the web ports.
DEFAULT_PORTS: tuple[int, ...] = (
    21,
    22,
    25,
    80,
    443,
    445,
    1433,
    3000,
    3306,
    3389,
    5432,
    6379,
    8000,
    8080,
    8443,
    9000,
    9200,
    27017,
)

#: URL-ish strings embedded in JavaScript, relative or absolute.
_JS_URL = re.compile(r"""["'`]((?:https?://|/)[^"'`\s<>]{2,200})["'`]""")

#: ``href="..."`` and ``src="..."`` pairs.
_HTML_LINK = re.compile(
    r"""(?:href|src|action|formaction|data-url|data-href|data-src)\s*=\s*["']([^"'#]{2,300})["']""",
    re.IGNORECASE,
)


@dataclass
class Surface:
    """Everything recon learned about one host."""

    host: str
    base_url: str = ""
    endpoints: list[str] = field(default_factory=list)
    forms: list[str] = field(default_factory=list)
    api_endpoints: list[str] = field(default_factory=list)
    technologies: dict[str, str] = field(default_factory=dict)
    robots_rules: list[str] = field(default_factory=list)
    sitemap_urls: list[str] = field(default_factory=list)
    open_ports: list[int] = field(default_factory=list)
    subdomains: list[str] = field(default_factory=list)

    def add_endpoint(self, url: str) -> bool:
        """Record an endpoint if it is new. Returns whether it was added."""
        if url in self.endpoints:
            return False
        self.endpoints.append(url)
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "base_url": self.base_url,
            "endpoints": list(self.endpoints),
            "forms": list(self.forms),
            "api_endpoints": list(self.api_endpoints),
            "technologies": dict(self.technologies),
            "robots_rules": list(self.robots_rules),
            "sitemap_urls": list(self.sitemap_urls),
            "open_ports": list(self.open_ports),
            "subdomains": list(self.subdomains),
        }


@dataclass
class CrawlStats:
    """What the crawl actually cost."""

    pages_fetched: int = 0
    requests_sent: int = 0
    max_depth_reached: int = 0
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class Crawler:
    """A bounded, same-origin crawler.

    Three properties make it safe to point at a target the operator owns
    but did not build:

    - **Bounded.** A page budget and a depth limit. The crawl stops
      even if the site is a maze.
    - **Same-origin.** Links to other hosts are recorded as *discovered*
      but not followed, so a site full of outbound links cannot drag
      the crawl off the authorised host.
    - **Non-destructive.** Only ``GET`` and ``HEAD`` are used. A crawler
      that submits forms is a scanner, not a crawler.
    """

    def __init__(
        self,
        prober: Prober,
        guard: ScopeGuard,
        *,
        max_pages: int = 200,
        max_depth: int = 3,
        include_patterns: tuple[str, ...] = (),
        exclude_patterns: tuple[str, ...] = (),
    ) -> None:
        self.prober = prober
        self.guard = guard
        self.max_pages = max_pages
        self.max_depth = max_depth
        self.include_patterns = include_patterns
        self.exclude_patterns = exclude_patterns
        self.stats = CrawlStats()
        self._seen: set[str] = set()
        self._queue: list[tuple[str, int]] = []

    async def crawl(self, start_url: str) -> Surface:
        """Crawl from ``start_url`` and return the mapped surface."""
        parsed = urlparse(start_url)
        surface = Surface(host=parsed.hostname or "", base_url=start_url)
        self._queue.append((start_url, 0))
        self._seen.add(self._normalise(start_url))

        while self._queue:
            if self.stats.pages_fetched >= self.max_pages:
                self.stats.truncated = True
                break

            url, depth = self._queue.pop(0)
            response = await self.prober.request(url, method="GET")
            self.stats.requests_sent += 1
            if response.error or not response.ok:
                continue

            self.stats.pages_fetched += 1
            self.stats.max_depth_reached = max(self.stats.max_depth_reached, depth)
            surface.add_endpoint(url)

            content_type = response.content_type.lower()
            if "html" not in content_type and "xml" not in content_type:
                continue
            if "html" not in content_type:
                for link in self._sitemap_links(response.body):
                    accepted = self._enqueue(surface, link, depth, url)
                    if accepted is not None:
                        self._queue.append((accepted, depth + 1))
                continue

            for link in self._extract_links(response.body, url):
                accepted = self._enqueue(surface, link, depth, url)
                if accepted is not None:
                    self._queue.append((accepted, depth + 1))

            for form in self._extract_forms(response.body, url):
                if form not in surface.forms:
                    surface.forms.append(form)

        return surface

    def _enqueue(self, surface: Surface, link: str, depth: int, origin: str) -> str | None:
        """The absolute URL to follow, or ``None`` when the link is refused.

        The caller queues what this returns rather than the link as it
        appeared in the page: a relative ``href`` fetched as-is would reach
        the prober without a scheme and be refused as malformed.
        """
        if depth >= self.max_depth:
            return None
        absolute = urljoin(origin, link)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            return None

        origin_host = urlparse(origin).hostname
        if parsed.hostname != origin_host:
            # A different host is interesting, but following it would
            # leave the authorised scope. Record the host and stop.
            if parsed.hostname and parsed.hostname not in surface.subdomains:
                surface.subdomains.append(parsed.hostname)
            return None

        path = parsed.path.lower()
        if any(path.endswith(extension) for extension in ASSET_EXTENSIONS):
            return None
        if any(marker in path for marker in EXCLUDED_PATH_MARKERS):
            return None
        if self.exclude_patterns and any(
            re.search(pattern, path) for pattern in self.exclude_patterns
        ):
            return None
        if self.include_patterns and not any(
            re.search(pattern, path) for pattern in self.include_patterns
        ):
            return None

        normalised = self._normalise(absolute)
        if normalised in self._seen:
            return None
        self._seen.add(normalised)
        surface.add_endpoint(normalised)
        return normalised

    @staticmethod
    def _normalise(url: str) -> str:
        """Strip the fragment so ``/a#x`` and ``/a`` are one URL."""
        parsed = urlparse(url)
        return parsed._replace(fragment="").geturl()

    @staticmethod
    def _extract_links(body: str, base_url: str) -> list[str]:
        """Every candidate link in an HTML body.

        Two passes: real ``href``/``src`` attributes first, then URL-ish
        strings inside scripts. The second pass finds endpoints that are
        only referenced from JavaScript, which is where modern
        single-page applications keep their API paths.
        """
        links: list[str] = []
        seen: set[str] = set()

        for match in _HTML_LINK.finditer(body):
            candidate = match.group(1).strip()
            if candidate and candidate not in seen:
                seen.add(candidate)
                links.append(candidate)

        for match in _JS_URL.finditer(body):
            candidate = match.group(1).strip()
            if candidate and candidate not in seen:
                seen.add(candidate)
                links.append(candidate)

        return links

    @staticmethod
    def _extract_forms(body: str, base_url: str) -> list[str]:
        """Form action URLs — the endpoints worth injecting into."""
        actions: list[str] = []
        for match in re.finditer(
            r"""<form[^>]*\baction\s*=\s*["']([^"']{1,300})["']""", body, re.IGNORECASE
        ):
            action = match.group(1).strip()
            if action:
                actions.append(urljoin(base_url, action))
        return actions

    @staticmethod
    def _sitemap_links(body: str) -> list[str]:
        """``<loc>`` entries from a sitemap document."""
        return [match.group(1).strip() for match in re.finditer(r"<loc>\s*([^<]+)\s*</loc>", body)]


async def fetch_robots(prober: Prober, base_url: str) -> tuple[list[str], list[str]]:
    """Fetch ``robots.txt`` and return ``(disallow_rules, sitemap_urls)``.

    A ``Disallow:`` entry is not a secret — it is the site admitting the
    path exists. That makes robots.txt one of the most reliable
    endpoint-discovery sources there is.
    """
    robots_url = urljoin(base_url.rstrip("/") + "/", "robots.txt")
    response = await prober.request(robots_url, method="GET")
    if response.error or not response.ok or "html" in response.content_type.lower():
        return [], []

    rules: list[str] = []
    sitemaps: list[str] = []
    for line in response.body.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "disallow" and value:
            rules.append(value)
        elif key == "sitemap" and value:
            sitemaps.append(value)
    return rules, sitemaps


async def fetch_sitemap(prober: Prober, sitemap_url: str, *, limit: int = 500) -> list[str]:
    """Fetch a sitemap (or sitemap index) and return the URLs it lists."""
    response = await prober.request(sitemap_url, method="GET")
    if response.error or not response.ok:
        return []

    body = response.body
    locs = [match.group(1).strip() for match in re.finditer(r"<loc>\s*([^<]+)\s*</loc>", body)]
    if not locs:
        return []

    # A sitemap index points at further sitemaps rather than at pages.
    if "<sitemapindex" in body.lower() and len(locs) <= 50:
        collected: list[str] = []
        for child in locs[:10]:
            collected.extend(await fetch_sitemap(prober, child, limit=limit - len(collected)))
            if len(collected) >= limit:
                break
        return collected[:limit]
    return locs[:limit]


@dataclass
class FingerprintRule:
    """One way to recognise a technology."""

    name: str
    pattern: re.Pattern[str]
    version_group: int = 0


#: Technology signatures, checked against headers, cookies, and body.
FINGERPRINT_RULES: tuple[FingerprintRule, ...] = (
    FingerprintRule(
        "nginx",
        re.compile(r"nginx(?:/([\d.]+))?", re.IGNORECASE),
        1,
    ),
    FingerprintRule("apache", re.compile(r"apache(?:/([\d.]+))?", re.IGNORECASE), 1),
    FingerprintRule("iis", re.compile(r"Microsoft-IIS(?:/([\d.]+))?", re.IGNORECASE), 1),
    FingerprintRule("caddy", re.compile(r"Caddy(?:/([\d.]+))?", re.IGNORECASE), 1),
    FingerprintRule("cloudflare", re.compile(r"cloudflare", re.IGNORECASE)),
    FingerprintRule("jetty", re.compile(r"Jetty\(([\d.]+)\)", re.IGNORECASE), 1),
    FingerprintRule("tomcat", re.compile(r"Apache-Coyote/([\d.]+)", re.IGNORECASE), 1),
    FingerprintRule("gunicorn", re.compile(r"gunicorn(?:/([\d.]+))?", re.IGNORECASE), 1),
    FingerprintRule("werkzeug", re.compile(r"Werkzeug(?:/([\d.]+))?", re.IGNORECASE), 1),
    FingerprintRule("flask", re.compile(r"werkzeug(?:/([\d.]+))?", re.IGNORECASE), 1),
    FingerprintRule("django", re.compile(r"Django(?:/([\d.]+))?", re.IGNORECASE), 1),
    FingerprintRule("express", re.compile(r"Express(?:/([\d.]+))?", re.IGNORECASE), 1),
    FingerprintRule("nextjs", re.compile(r"Next\.js(?: - ([^\s]+))?", re.IGNORECASE), 1),
    FingerprintRule("rails", re.compile(r"rails(?:/([\d.]+))?", re.IGNORECASE), 1),
    FingerprintRule("laravel", re.compile(r"Laravel(?:/([\d.]+))?", re.IGNORECASE), 1),
    FingerprintRule("spring-boot", re.compile(r"spring-boot(?:/([\d.]+))?", re.IGNORECASE), 1),
    FingerprintRule("phpmyadmin", re.compile(r"phpMyAdmin(?:/([\d.]+))?", re.IGNORECASE), 1),
    FingerprintRule("cloudfront", re.compile(r"CloudFront", re.IGNORECASE)),
    FingerprintRule("akamai", re.compile(r"akamai(?:Ghost|NetStorage)?", re.IGNORECASE)),
)

#: Body markers for frameworks that hide in the HTML rather than headers.
BODY_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("wordpress", re.compile(r"/wp-content/|wp-includes/", re.IGNORECASE)),
    ("drupal", re.compile(r"/sites/default/files|Drupal\.settings", re.IGNORECASE)),
    ("joomla", re.compile(r"/media/jui/|com_content", re.IGNORECASE)),
    ("react", re.compile(r"data-reactroot|__REACT_DEVTOOLS", re.IGNORECASE)),
    ("vue", re.compile(r"data-v-[0-9a-f]{8}|__VUE__", re.IGNORECASE)),
    ("angular", re.compile(r"ng-version=|ng-app", re.IGNORECASE)),
    ("svelte", re.compile(r"svelte-[0-9a-z]{6}", re.IGNORECASE)),
    ("tailwind", re.compile(r"tailwindcss|tailwind-", re.IGNORECASE)),
    ("bootstrap", re.compile(r"bootstrap(?:.min)?\.(?:css|js)", re.IGNORECASE)),
    ("graphql", re.compile(r"/graphql", re.IGNORECASE)),
    ("swagger-ui", re.compile(r"swagger-ui|swagger\.js", re.IGNORECASE)),
    ("phpmyadmin", re.compile(r"phpmyadmin", re.IGNORECASE)),
    ("cloudflare", re.compile(r"cdn-cgi/", re.IGNORECASE)),
)


def fingerprint(response: Response) -> dict[str, str]:
    """Identify the technologies behind one response.

    Returns a mapping of technology name to version (or an empty string
    when the version is unknown). The version matters more than the
    name: it is what turns "it runs WordPress" into "it runs WordPress
    4.7, which is vulnerable to CVE-2017-5487".
    """
    found: dict[str, str] = {}

    header_blob = " ".join(f"{key}: {value}" for key, value in response.headers.items())
    cookie_blob = " ".join(response.header_values("set-cookie"))
    body_blob = response.body[:200_000]

    for rule in FINGERPRINT_RULES:
        match = rule.pattern.search(header_blob) or rule.pattern.search(cookie_blob)
        if match is None:
            continue
        version = ""
        if rule.version_group:
            try:
                version = match.group(rule.version_group) or ""
            except IndexError:  # pragma: no cover - defensive
                version = ""
        found[rule.name] = version

    for name, pattern in BODY_MARKERS:
        if pattern.search(body_blob):
            found.setdefault(name, "")

    return found


@dataclass
class PortScanResult:
    """The outcome of a TCP connect scan."""

    host: str
    open_ports: list[int] = field(default_factory=list)
    filtered_ports: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "open_ports": list(self.open_ports),
            "filtered_ports": list(self.filtered_ports),
            "errors": list(self.errors),
        }


class PortScanner:
    """TCP connect scan — the least noisy port probe there is.

    A connect scan opens a full TCP handshake, so the target sees a
    normal connection that it may log, but it never receives a
    malformed packet. That matters for a scan the operator intends to
    explain afterwards.
    """

    def __init__(
        self,
        *,
        timeout: float = 2.0,
        concurrency: int = 20,
        allowed_ports: list[int] | None = None,
    ) -> None:
        self.timeout = timeout
        self.concurrency = concurrency
        self.allowed_ports = allowed_ports

    async def scan(self, host: str, ports: tuple[int, ...] = DEFAULT_PORTS) -> PortScanResult:
        """Scan ``host`` on ``ports`` and report what answered."""
        candidates = list(ports)
        if self.allowed_ports is not None:
            permitted = set(self.allowed_ports)
            candidates = [port for port in candidates if port in permitted]

        semaphore = asyncio.Semaphore(self.concurrency)
        result = PortScanResult(host=host)

        async def probe(port: int) -> tuple[int, bool, str]:
            async with semaphore:
                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(host, port), timeout=self.timeout
                    )
                except (TimeoutError, OSError) as exc:
                    return port, False, f"{type(exc).__name__}"
                writer.close()
                try:
                    await writer.wait_closed()
                except (OSError, TimeoutError):
                    pass
                del reader
                return port, True, ""

        outcomes = await asyncio.gather(*(probe(port) for port in candidates))
        for port, is_open, error in outcomes:
            if is_open:
                result.open_ports.append(port)
            elif error and "Timeout" not in error:
                result.errors.append(f"{port}: {error}")
        result.open_ports.sort()
        return result


async def resolve_host(host: str) -> str:
    """Resolve a hostname to an address, returning ``""`` on failure."""
    loop = asyncio.get_running_loop()
    try:
        result = await loop.getaddrinfo(host, None)
    except (socket.gaierror, OSError):
        return ""
    if not result:
        return ""
    address = result[0][4][0]
    return address if isinstance(address, str) else ""


class SubdomainEnumerator:
    """Wordlist-based subdomain discovery over DNS.

    Only names that resolve *and* fall inside the scope guard are
    returned as scannable. A name that resolves but is out of scope is
    still worth reporting — the operator may not have known it existed —
    but it is never handed to the scanner.
    """

    def __init__(
        self,
        guard: ScopeGuard,
        *,
        words: tuple[str, ...] = DEFAULT_SUBDOMAIN_WORDS,
        resolve_concurrently: int = 20,
    ) -> None:
        self.guard = guard
        self.words = words
        self.resolve_concurrently = resolve_concurrently

    async def enumerate(self, domain: str) -> tuple[list[str], list[str]]:
        """Return ``(in_scope_hosts, out_of_scope_hosts)``."""
        candidates = [f"{word}.{domain}" for word in self.words]
        semaphore = asyncio.Semaphore(self.resolve_concurrently)

        async def check(candidate: str) -> tuple[str, bool]:
            async with semaphore:
                address = await resolve_host(candidate)
                return candidate, bool(address)

        results = await asyncio.gather(*(check(candidate) for candidate in candidates))

        in_scope: list[str] = []
        out_of_scope: list[str] = []
        for candidate, resolved in results:
            if not resolved:
                continue
            if self.guard.host_allowed(candidate):
                in_scope.append(candidate)
            else:
                out_of_scope.append(candidate)
        return in_scope, out_of_scope


def build_recon_findings(
    surface: Surface,
    *,
    out_of_scope_hosts: list[str] | None = None,
    open_ports: list[int] | None = None,
) -> list[Finding]:
    """Turn recon observations into findings.

    Recon observations are findings in their own right: an open database
    port, an out-of-scope host that the organisation owns, and an
    exposed admin panel are all things a human needs to know about
    even though nobody exploited them.
    """
    findings: list[Finding] = []

    for host in out_of_scope_hosts or []:
        findings.append(
            Finding(
                title=f"Subdomain outside the authorised scope: {host}",
                severity=Severity.LOW,
                category=Category.RECON,
                confidence=Confidence.CONFIRMED,
                target=surface.host,
                description=(
                    f"The hostname {host} resolves, but it is not in the "
                    "authorised scope and was therefore not scanned."
                ),
                impact=(
                    "An asset outside the agreed scope is a blind spot: it may "
                    "host an older build of the same application with weaker "
                    "controls, and it is exactly the kind of host an attacker "
                    "reaches for first."
                ),
                remediation=(
                    "Confirm whether this host is in scope, then either add it "
                    "to the scan target or take it out of DNS."
                ),
                cwe="CWE-200",
                owasp="A05:2021 - Security Misconfiguration",
                evidence=[
                    Evidence(
                        kind=EvidenceKind.MATCH,
                        summary=f"DNS resolution succeeded for {host}",
                    )
                ],
                source="recon-subdomains",
                tags=["recon", "subdomain", "scope"],
            )
        )

    for port in open_ports or []:
        findings.append(
            Finding(
                title=f"Service port {port} is open on {surface.host}",
                severity=Severity.LOW,
                category=Category.RECON,
                confidence=Confidence.CONFIRMED,
                target=surface.host,
                description=f"TCP port {port} accepted a connection on {surface.host}.",
                impact=(
                    "Every open port is a service an attacker can speak to. "
                    "Administrative interfaces and database ports on a public "
                    "host are a large share of successful intrusions."
                ),
                remediation=(
                    "Close the port to the public internet, or restrict it to "
                    "the addresses that legitimately need it with a firewall rule."
                ),
                cwe="CWE-284",
                owasp="A05:2021 - Security Misconfiguration",
                evidence=[
                    Evidence(
                        kind=EvidenceKind.MATCH,
                        summary=f"TCP connect to {surface.host}:{port} succeeded",
                    )
                ],
                source="recon-ports",
                tags=["recon", "port", "exposure"],
            )
        )

    for path in surface.robots_rules:
        if path in ("/", "") or path.startswith("/api") or path.startswith("/admin"):
            findings.append(
                Finding(
                    title=f"Disallowed path published in robots.txt: {path}",
                    severity=Severity.LOW,
                    category=Category.RECON,
                    confidence=Confidence.CONFIRMED,
                    target=surface.host,
                    description=(
                        f"robots.txt lists '{path}' as disallowed, which confirms "
                        "the path exists and is worth an authenticated probe."
                    ),
                    impact=(
                        "The path is reachable — robots.txt controls crawlers, "
                        "not browsers. Direct requests bypass it entirely."
                    ),
                    remediation=(
                        "Do not rely on robots.txt for access control. Require "
                        "authentication on the path itself."
                    ),
                    cwe="CWE-284",
                    owasp="A05:2021 - Security Misconfiguration",
                    evidence=[
                        Evidence(
                            kind=EvidenceKind.MATCH,
                            summary=f"robots.txt: Disallow: {path}",
                        )
                    ],
                    source="recon-robots",
                    tags=["recon", "robots", "discovery"],
                )
            )

    return findings
