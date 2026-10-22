"""Scope guard — the hard boundary on what an autonomous scan may touch.

An autonomous security agent that runs without a human in the loop is
only responsible if its reach is *mechanically* bounded. This module is
that bound:

- **Explicit scope.** Only hosts the operator listed may be contacted.
  Everything else is refused, including hosts discovered mid-scan. A
  redirect to another domain does not widen the scope.
- **Bounded requests.** A request budget, a per-host rate limit, and a
  concurrency cap. The scanner cannot turn into a flooder, because the
  guard rejects the request before it reaches the network.
- **Bounded time.** An overall deadline. A scan that stalls stops.
- **No unsafe destinations.** Loopback, private, and link-local addresses
  are refused by default, so a compromised or confused agent cannot be
  steered into probing the host it runs on or its neighbours.
- **No unsafe verbs.** Only the methods in the policy's allowlist may be
  sent. A write verb (``PUT``, ``DELETE``, ``PATCH``) is refused *before*
  it reaches the network, because a check that tests for an open write
  method by writing has already caused the damage it was looking for.

The guard is deliberately *separate* from the permission gate. The gate
asks "should this human approve this action?"; the scope guard answers
"is this destination even allowed?" and is enforced for every request
regardless of permission mode — including ``allow-all``.
"""

from __future__ import annotations

import ipaddress
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlparse


class ScopeViolation(str, Enum):
    """Why a request was refused. Every refusal names its reason."""

    NOT_IN_SCOPE = "not-in-scope"
    SCHEME_NOT_ALLOWED = "scheme-not-allowed"
    PORT_NOT_ALLOWED = "port-not-allowed"
    PATH_NOT_ALLOWED = "path-not-allowed"
    PRIVATE_ADDRESS = "private-address"
    BUDGET_EXHAUSTED = "budget-exhausted"
    RATE_LIMITED = "rate-limited"
    DEADLINE_PASSED = "deadline-passed"
    MALFORMED_URL = "malformed-url"
    METHOD_NOT_ALLOWED = "method-not-allowed"


class ScopeError(RuntimeError):
    """Raised when a request falls outside the authorised scope."""

    def __init__(self, violation: ScopeViolation, detail: str) -> None:
        super().__init__(f"{violation.value}: {detail}")
        self.violation = violation
        self.detail = detail


#: Schemes a web scan may speak. Anything else (file://, gopher://, ftp://)
#: is refused — a scan that can read local files is a scan that can be
#: steered into exfiltrating the operator's own machine.
ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

#: Verbs a scan may send unless its policy says otherwise.
#:
#: ``GET``, ``HEAD``, and ``OPTIONS`` are reads. ``POST`` is included
#: because form-shaped injection probes need a request body — the engine
#: only ever sends read-only bodies. ``TRACE`` and ``PROPFIND`` are here
#: for the configuration check that asks whether they are enabled; both
#: read, neither writes.
#:
#: ``PUT``, ``DELETE``, ``PATCH``, and ``CONNECT`` are absent on purpose.
#: An assessment must not test a write method by performing the write.
ALLOWED_METHODS: frozenset[str] = frozenset(
    {"GET", "HEAD", "OPTIONS", "POST", "TRACE", "PROPFIND"}
)

#: Default ports per scheme, used when a URL omits one.
DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}

_HOST_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")


def _is_hostname(host: str) -> bool:
    """Whether a string is a syntactically valid DNS hostname."""
    return bool(host) and len(host) <= 253 and bool(_HOST_PATTERN.match(host))


def _is_blocked_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether an address is one a scan has no business touching.

    Loopback is the operator's own machine, private ranges are their
    network, link-local covers cloud metadata services (169.254.169.254)
    and carrier-grade NAT, and multicast/reserved are never a target.
    """
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


@dataclass
class ScopePolicy:
    """The operator's authorisation, expressed as data.

    ``allowed_hosts`` accepts exact hosts (``app.example.com``) and
    wildcard suffixes (``*.example.com``). ``allowed_paths`` narrows
    further; an empty list means "any path on an allowed host".
    ``allowed_methods`` is the verb allowlist: a request using a method
    outside it is refused before it is sent, whatever the destination.
    """

    allowed_hosts: list[str] = field(default_factory=list)
    allowed_paths: list[str] = field(default_factory=list)
    allowed_methods: frozenset[str] = field(default_factory=lambda: ALLOWED_METHODS)
    allowed_schemes: list[str] = field(default_factory=lambda: ["http", "https"])
    allowed_ports: list[int] = field(default_factory=lambda: [80, 443, 8080, 8443, 3000, 5000])
    max_requests: int = 2000
    max_requests_per_host: int = 400
    requests_per_second: float = 10.0
    max_concurrency: int = 6
    timeout_seconds: float = 1800.0
    allow_private_addresses: bool = False
    follow_redirects: bool = True
    user_agent: str = "AegisX-Cyber/1.0 (+authorized-security-testing)"

    def __post_init__(self) -> None:
        self.allowed_hosts = [host.lower().strip() for host in self.allowed_hosts if host.strip()]
        self.allowed_paths = [
            path if path.startswith("/") else f"/{path}" for path in self.allowed_paths
        ]
        self.allowed_schemes = [scheme.lower() for scheme in self.allowed_schemes]
        self.allowed_methods = frozenset(
            str(method).strip().upper() for method in self.allowed_methods if str(method).strip()
        )
        if not self.allowed_methods:
            raise ValueError("allowed_methods must name at least one HTTP method")
        if self.requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        if self.max_requests < 1:
            raise ValueError("max_requests must be at least 1")


@dataclass
class ScopeDecision:
    """The verdict for one candidate URL."""

    allowed: bool
    url: str
    host: str
    violation: ScopeViolation | None = None
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


class ScopeGuard:
    """Enforces the authorisation boundary for every outgoing request.

    One instance is shared by the whole scan. It is stateful in exactly
    one way: it counts what has been sent, so the request budget and the
    per-host limit are real rather than advisory.
    """

    def __init__(self, policy: ScopePolicy) -> None:
        self.policy = policy
        self._requests_made = 0
        self._per_host: dict[str, int] = {}
        self._started_at = time.monotonic()
        self._last_request_at = 0.0
        self._violations: list[tuple[str, str]] = []

    # === Introspection ================================================= #

    @property
    def requests_made(self) -> int:
        return self._requests_made

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._started_at

    @property
    def remaining_requests(self) -> int:
        return max(0, self.policy.max_requests - self._requests_made)

    @property
    def violations(self) -> list[tuple[str, str]]:
        """Every refusal, as ``(url, reason)`` — part of the audit trail."""
        return list(self._violations)

    def host_allowed(self, host: str) -> bool:
        """Whether a host matches the allowlist (exact or wildcard)."""
        candidate = host.lower().rstrip(".")
        for pattern in self.policy.allowed_hosts:
            if pattern.startswith("*."):
                suffix = pattern[1:]  # ".example.com"
                if candidate == pattern[2:] or candidate.endswith(suffix):
                    return True
            elif candidate == pattern:
                return True
        return False

    def allows_method(self, method: str) -> bool:
        """Whether the policy permits this HTTP verb.

        Checks consult this before choosing a probe: a verb the policy
        refuses would come back as a scope violation, which is a wasted
        request and a misleading error in the scan's stats.
        """
        return method.strip().upper() in self.policy.allowed_methods

    def path_allowed(self, path: str) -> bool:
        """Whether a path is inside the authorised path prefix."""
        if not self.policy.allowed_paths:
            return True
        for prefix in self.policy.allowed_paths:
            if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
                return True
            if prefix.endswith("*") and path.startswith(prefix[:-1]):
                return True
        return False

    # === The check ===================================================== #

    def check(self, url: str, *, method: str = "GET") -> ScopeDecision:
        """Decide whether a request may be made, without making it.

        This is pure: it consults the policy and the counters but performs
        no I/O, so it can be unit-tested exhaustively and reused as a
        pre-flight check in the prober.
        """
        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()
        host = (parsed.hostname or "").lower().rstrip(".")

        if not scheme or not host:
            self._violations.append((url, ScopeViolation.MALFORMED_URL.value))
            return ScopeDecision(
                False, url, host, ScopeViolation.MALFORMED_URL, "missing scheme or host"
            )

        # The verb is checked before the destination: an unsafe method is a
        # refusal no matter which host it is aimed at, and every violation
        # is recorded, so a caller that keeps asking keeps being told.
        verb = (method or "GET").strip().upper()
        if verb not in self.policy.allowed_methods:
            reason = (
                f"method {verb!r} is not one of {sorted(self.policy.allowed_methods)}"
            )
            self._violations.append((url, ScopeViolation.METHOD_NOT_ALLOWED.value))
            return ScopeDecision(
                False, url, host, ScopeViolation.METHOD_NOT_ALLOWED, reason
            )

        if scheme not in self.policy.allowed_schemes:
            reason = f"scheme {scheme!r} is not in {self.policy.allowed_schemes}"
            self._violations.append((url, ScopeViolation.SCHEME_NOT_ALLOWED.value))
            return ScopeDecision(False, url, host, ScopeViolation.SCHEME_NOT_ALLOWED, reason)

        if not self.host_allowed(host):
            reason = f"host {host!r} is not in the authorised scope"
            self._violations.append((url, ScopeViolation.NOT_IN_SCOPE.value))
            return ScopeDecision(False, url, host, ScopeViolation.NOT_IN_SCOPE, reason)

        port = parsed.port or DEFAULT_PORTS.get(scheme, 80)
        if port not in self.policy.allowed_ports:
            reason = f"port {port} is not in {self.policy.allowed_ports}"
            self._violations.append((url, ScopeViolation.PORT_NOT_ALLOWED.value))
            return ScopeDecision(False, url, host, ScopeViolation.PORT_NOT_ALLOWED, reason)

        path = parsed.path or "/"
        if not self.path_allowed(path):
            reason = f"path {path!r} is outside the authorised paths"
            self._violations.append((url, ScopeViolation.PATH_NOT_ALLOWED.value))
            return ScopeDecision(False, url, host, ScopeViolation.PATH_NOT_ALLOWED, reason)

        if not self.policy.allow_private_addresses:
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                address = None
            if address is not None and _is_blocked_ip(address):
                reason = f"{host} resolves to a non-public address"
                self._violations.append((url, ScopeViolation.PRIVATE_ADDRESS.value))
                return ScopeDecision(False, url, host, ScopeViolation.PRIVATE_ADDRESS, reason)

        if self.elapsed_seconds > self.policy.timeout_seconds:
            reason = f"scan deadline of {self.policy.timeout_seconds}s exceeded"
            self._violations.append((url, ScopeViolation.DEADLINE_PASSED.value))
            return ScopeDecision(False, url, host, ScopeViolation.DEADLINE_PASSED, reason)

        if self.remaining_requests <= 0:
            reason = f"request budget of {self.policy.max_requests} is exhausted"
            self._violations.append((url, ScopeViolation.BUDGET_EXHAUSTED.value))
            return ScopeDecision(False, url, host, ScopeViolation.BUDGET_EXHAUSTED, reason)

        used_for_host = self._per_host.get(host, 0)
        if used_for_host >= self.policy.max_requests_per_host:
            reason = (
                f"per-host budget of {self.policy.max_requests_per_host} is exhausted for {host}"
            )
            self._violations.append((url, ScopeViolation.BUDGET_EXHAUSTED.value))
            return ScopeDecision(False, url, host, ScopeViolation.BUDGET_EXHAUSTED, reason)

        return ScopeDecision(True, url, host)

    def record_request(self, url: str) -> None:
        """Count a request that was actually sent."""
        self._requests_made += 1
        host = (urlparse(url).hostname or "").lower()
        self._per_host[host] = self._per_host.get(host, 0) + 1

    async def throttle(self) -> None:
        """Sleep just enough to honour the requests-per-second limit."""
        import asyncio

        interval = 1.0 / self.policy.requests_per_second
        now = time.monotonic()
        wait = self._last_request_at + interval - now
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_at = time.monotonic()

    def ensure_allowed(self, url: str, *, method: str = "GET") -> None:
        """Raise :class:`ScopeError` unless the URL is authorised."""
        decision = self.check(url, method=method)
        if not decision.allowed:
            raise ScopeError(decision.violation or ScopeViolation.NOT_IN_SCOPE, decision.reason)


def scope_from_targets(
    targets: list[str],
    *,
    extra_hosts: list[str] | None = None,
    **overrides: object,
) -> ScopePolicy:
    """Build a policy from a list of target URLs.

    The operator's targets are the scope. Hosts discovered *during* the
    scan are never added automatically — that would let a redirect or a
    certificate SAN silently widen the blast radius.
    """
    hosts: list[str] = []
    ports: set[int] = set()
    for target in targets:
        parsed = urlparse(target if "://" in target else f"https://{target}")
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host:
            continue
        if not _is_hostname(host) and not _looks_like_ip(host):
            raise ValueError(f"{target!r} is not a valid host or IP address")
        if host not in hosts:
            hosts.append(host)
        port = parsed.port or DEFAULT_PORTS.get((parsed.scheme or "https").lower(), 443)
        ports.add(port)
    for host in extra_hosts or []:
        clean = host.lower().strip()
        if clean and clean not in hosts:
            hosts.append(clean)
    return ScopePolicy(
        allowed_hosts=hosts,
        allowed_ports=sorted(ports) or [80, 443],
        **overrides,  # type: ignore[arg-type]
    )


def _looks_like_ip(host: str) -> bool:
    """Whether a host string is a literal IPv4 or IPv6 address."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True
