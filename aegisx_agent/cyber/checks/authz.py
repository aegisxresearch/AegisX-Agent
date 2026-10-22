"""Authorization and transport checks — IDOR, HTTP methods, redirects, TLS.

Broken access control is the most common serious finding in a web
assessment, and the one most likely to be missed by a scanner that only
looks for injection. These checks look for the shape of the problem:
an identifier in the path that the server does not actually check
against the caller's identity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from aegisx_agent.cyber.checks import CheckContext, finding
from aegisx_agent.cyber.models import (
    Category,
    Confidence,
    Evidence,
    EvidenceKind,
    Finding,
    Severity,
)

if TYPE_CHECKING:  # avoids a runtime import cycle
    from aegisx_agent.cyber.prober import Response

#: Paths whose last segment looks like an object identifier.
_ID_SEGMENT = re.compile(
    r"^(?P<id>\d{1,12}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    re.IGNORECASE,
)

#: URL segments that identify a user, a record, or a resource.
_ID_ENDPOINT_MARKERS: tuple[str, ...] = (
    "/user/",
    "/users/",
    "/account/",
    "/accounts/",
    "/profile/",
    "/profiles/",
    "/order/",
    "/orders/",
    "/invoice/",
    "/invoices/",
    "/payment/",
    "/payments/",
    "/customer/",
    "/customers/",
    "/document/",
    "/documents/",
    "/file/",
    "/files/",
    "/ticket/",
    "/tickets/",
    "/api/",
    "/rest/",
    "/v1/",
    "/v2/",
)


@dataclass
class IDORCheck:
    """Look for object identifiers that are not bound to the caller.

    The test swaps the identifier in a path for a neighbouring one and
    compares the responses. If a different object's data comes back with
    the same status, the server is not authorising access to that
    object — it is only checking that *some* request arrived.

    This is a **tentative** finding by construction: without a second
    identity to compare against, the scanner cannot prove the second
    object belongs to someone else. It reports the shape of the problem
    so a tester with credentials can confirm it in one request.
    """

    name: str = "idor"
    category: Category = Category.AUTHZ
    cost: str = "deep"

    async def run(self, context: CheckContext) -> list[Finding]:
        found: list[Finding] = []
        for endpoint in context.endpoints:
            candidate = self._identifier_endpoint(endpoint)
            if candidate is None:
                continue
            path, identifier, replacement = candidate
            if replacement == identifier:
                continue

            original = await context.fetch(endpoint)
            if not original.ok or original.status != 200:
                continue
            if not original.body.strip():
                continue

            swapped_url = endpoint.replace(f"/{identifier}", f"/{replacement}", 1)
            swapped = await context.fetch(swapped_url)
            if swapped.error or swapped.status != 200:
                continue
            if swapped.body.strip() == original.body.strip():
                continue

            found.append(
                finding(
                    title=f"Object identifier in path is not access-controlled ({identifier})",
                    severity=Severity.MEDIUM,
                    category=self.category,
                    confidence=Confidence.TENTATIVE,
                    context=context,
                    endpoint=endpoint,
                    description=(
                        f"Requesting {path} with the identifier changed from "
                        f"{identifier} to {replacement} returned a different HTTP 200 "
                        "response instead of a denial. The endpoint appears to serve "
                        "objects by identifier without checking that the caller owns "
                        "them."
                    ),
                    impact=(
                        "If the second object belongs to another account, any "
                        "authenticated user can read other users' records by "
                        "changing one number in the URL — horizontal privilege "
                        "escalation across the whole user base."
                    ),
                    remediation=(
                        "Authorise every request against the object being accessed, "
                        "not merely against the session. Look the resource up scoped "
                        "by the authenticated principal's identifier so a foreign "
                        "identifier simply does not exist."
                    ),
                    cwe="CWE-639",
                    owasp="A01:2021 - Broken Access Control",
                    cvss=7.5,
                    references=["https://owasp.org/Top10/A01_2021-Broken_Access_Control/"],
                    evidence=[
                        Evidence(
                            kind=EvidenceKind.RESPONSE,
                            summary=(
                                f"Identifier {identifier} -> {replacement} returned "
                                f"HTTP {swapped.status} with different content"
                            ),
                            request=swapped.request_line(),
                            response_status=swapped.status,
                            snippet=swapped.body[:400],
                        )
                    ],
                    source=self.name,
                    tags=["idor", "authz", "broken-access-control"],
                )
            )
        return found

    @staticmethod
    def _identifier_endpoint(endpoint: str) -> tuple[str, str, str] | None:
        """Find an endpoint whose last path segment is an identifier."""
        parsed = urlparse(endpoint)
        if not parsed.path:
            return None
        segments = [segment for segment in parsed.path.split("/") if segment]
        if len(segments) < 2:
            return None
        candidate = segments[-1]
        match = _ID_SEGMENT.match(candidate)
        if not match:
            return None
        lowered = "/" + "/".join(segments[:-1]).lower() + "/"
        if not any(marker in lowered for marker in _ID_ENDPOINT_MARKERS):
            return None
        identifier = match.group("id")
        if identifier.isdigit():
            replacement = str(int(identifier) + 1)
        else:
            replacement = "00000000-0000-0000-0000-000000000000"
        return parsed.path, identifier, replacement


@dataclass
class HTTPMethodCheck:
    """Detect dangerous HTTP methods that should not be enabled.

    Read-only verbs are probed directly. Write verbs are **never sent**:
    the check asks ``OPTIONS`` what the server advertises instead, and
    reports an advertisement as ``probable`` rather than ``confirmed``,
    because the method was not exercised.

    That split is the whole point of the check. Testing whether ``DELETE``
    is enabled by sending ``DELETE`` has already performed the deletion it
    was hired to find — on a target the operator may only partly control.
    """

    name: str = "dangerous-methods"
    category: Category = Category.CONFIG
    cost: str = "cheap"

    #: Read-only verbs worth probing, and what an enabled one means. Neither
    #: changes state, so sending one is safe.
    PROBES: tuple[tuple[str, str, Severity], ...] = (
        (
            "TRACE",
            "TRACE echoes the request back, which can expose internal headers and cookies",
            Severity.MEDIUM,
        ),
        (
            "PROPFIND",
            "PROPFIND is a WebDAV method that enumerates server-side files",
            Severity.MEDIUM,
        ),
    )

    #: Write verbs, detected from the ``Allow`` header and never sent.
    WRITE_METHODS: tuple[tuple[str, str, Severity], ...] = (
        ("PUT", "PUT lets a client write files to the server", Severity.LOW),
        ("DELETE", "DELETE lets a client remove server-side resources", Severity.LOW),
    )

    async def run(self, context: CheckContext) -> list[Finding]:
        found = await self._probe_read_verbs(context)
        found.extend(await self._read_advertised_writes(context))
        return found

    async def _probe_read_verbs(self, context: CheckContext) -> list[Finding]:
        """Send the read-only probes and report the ones the server accepts."""
        found: list[Finding] = []
        for method, why, severity in self.PROBES:
            if not context.prober.guard.allows_method(method):
                continue
            response = await context.fetch(context.base_url, method=method)
            if response.error:
                continue
            if response.status not in (200, 201, 204):
                continue
            found.append(
                finding(
                    title=f"HTTP method {method} is enabled",
                    severity=severity,
                    category=self.category,
                    confidence=Confidence.CONFIRMED,
                    context=context,
                    description=f"The server accepts {method} requests. {why}.",
                    impact=(
                        "An exposed enumeration method widens the attack surface "
                        "beyond what the application intends, and can reveal files "
                        "the deployment did not mean to publish."
                    ),
                    remediation=(
                        f"Disable the {method} method for this location in the web "
                        "server or gateway, and expose only the verbs the API "
                        "actually implements."
                    ),
                    cwe="CWE-749",
                    owasp="A05:2021 - Security Misconfiguration",
                    evidence=[
                        Evidence(
                            kind=EvidenceKind.RESPONSE,
                            summary=f"{method} returned HTTP {response.status}",
                            request=response.request_line(),
                            response_status=response.status,
                        )
                    ],
                    source=self.name,
                    tags=["config", "http-method", method.lower()],
                )
            )
        return found

    async def _read_advertised_writes(self, context: CheckContext) -> list[Finding]:
        """Report write verbs the server advertises, without ever sending one."""
        if not context.prober.guard.allows_method("OPTIONS"):
            return []

        # A 405 or 501 is not a failure here: RFC 9110 says the Allow header
        # comes back with those too, and that header is the whole point.
        response = await context.fetch(context.base_url, method="OPTIONS")
        if response.error:
            return []

        advertised = self._parse_allow(response)
        if not advertised:
            return []

        found: list[Finding] = []
        allow_value = ", ".join(sorted(advertised))
        for method, why, severity in self.WRITE_METHODS:
            if method not in advertised:
                continue
            found.append(
                finding(
                    title=f"HTTP method {method} is advertised",
                    severity=severity,
                    category=self.category,
                    confidence=Confidence.PROBABLE,
                    context=context,
                    description=(
                        f"The server's Allow header lists {method}. {why}. The method "
                        "was not exercised — a write verb is not tested by writing."
                    ),
                    impact=(
                        "If the advertisement is accurate, a client can place or "
                        "remove server-side resources through the web server rather "
                        "than through the application."
                    ),
                    remediation=(
                        f"Disable the {method} method for this location in the web "
                        "server or gateway, then confirm that OPTIONS no longer "
                        "advertises it."
                    ),
                    cwe="CWE-749",
                    owasp="A05:2021 - Security Misconfiguration",
                    evidence=[
                        Evidence(
                            kind=EvidenceKind.RESPONSE,
                            summary=f"OPTIONS advertised {method} in its Allow header",
                            snippet=f"Allow: {allow_value}",
                            request=response.request_line(),
                            response_status=response.status,
                        )
                    ],
                    source=self.name,
                    tags=["config", "http-method", method.lower(), "advertised"],
                )
            )
        return found

    @staticmethod
    def _parse_allow(response: Response) -> set[str]:
        """Every method named in the response's Allow header(s), upper-cased."""
        values = response.header_values("allow") or [response.header("allow")]
        methods: set[str] = set()
        for value in values:
            methods.update(
                token.strip().upper()
                for token in str(value).replace(",", " ").split()
                if token.strip()
            )
        return methods


@dataclass
class OpenRedirectCheck:
    """Detect parameters that redirect to an arbitrary external URL.

    The probe passes a unique external host and looks for it in a
    ``Location`` header. Redirecting to an attacker-controlled domain is
    a credential-phishing primitive: the link is served from the trusted
    origin, so users and mail filters treat it as safe.
    """

    name: str = "open-redirect"
    category: Category = Category.CONFIG
    cost: str = "cheap"

    #: Parameter names that commonly carry a redirect destination.
    PARAM_NAMES: tuple[str, ...] = (
        "url",
        "next",
        "redirect",
        "redirect_uri",
        "return",
        "returnurl",
        "continue",
        "goto",
        "target",
        "dest",
        "destination",
        "redir",
        "callback",
    )

    async def run(self, context: CheckContext) -> list[Finding]:
        found: list[Finding] = []
        for endpoint in context.endpoints:
            parsed = urlparse(endpoint)
            if not parsed.query:
                continue
            pairs = parsed.query.split("&")
            for pair in pairs:
                name, _, _ = pair.partition("=")
                if name.lower() not in self.PARAM_NAMES:
                    continue
                probe_url = (
                    f"{endpoint.split('?')[0]}?{parsed.query}&{name}=https://aegisx-probe.invalid/x"
                )
                response = await context.fetch(probe_url, follow_redirects=False)
                location = response.header("location", "")
                if "aegisx-probe.invalid" not in location:
                    continue
                found.append(
                    finding(
                        title=f"Open redirect via parameter '{name}'",
                        severity=Severity.MEDIUM,
                        category=self.category,
                        confidence=Confidence.CONFIRMED,
                        context=context,
                        endpoint=endpoint,
                        description=(
                            f"The parameter '{name}' on {endpoint} is used to build a "
                            "redirect without validating the destination, and the "
                            "server redirected to the supplied external URL."
                        ),
                        impact=(
                            "A trusted-looking link on this origin that lands on an "
                            "attacker's page. Used against a login or password-reset "
                            "flow it captures credentials, and it defeats URL "
                            "reputation filters because the visible origin is real."
                        ),
                        remediation=(
                            "Validate redirect destinations against a strict "
                            "allow-list of relative paths or known hosts, and reject "
                            "anything containing a scheme or '//'."
                        ),
                        cwe="CWE-601",
                        owasp="A01:2021 - Broken Access Control",
                        cvss=6.1,
                        evidence=[
                            Evidence(
                                kind=EvidenceKind.RESPONSE,
                                summary=f"Location: {location}",
                                request=response.request_line(),
                                response_status=response.status,
                            )
                        ],
                        source=self.name,
                        tags=["redirect", "phishing", name],
                    )
                )
                break
        return found


@dataclass
class TransportCheck:
    """Check TLS configuration: encryption, HSTS, and certificate validity."""

    name: str = "transport-security"
    category: Category = Category.TRANSPORT
    cost: str = "cheap"

    async def run(self, context: CheckContext) -> list[Finding]:
        found: list[Finding] = []
        parsed = urlparse(context.base_url)
        if parsed.scheme != "https":
            found.append(
                finding(
                    title="Target does not use HTTPS",
                    severity=Severity.MEDIUM,
                    category=self.category,
                    confidence=Confidence.CONFIRMED,
                    context=context,
                    description=(
                        f"The scanned URL uses {parsed.scheme}://, so credentials and "
                        "session tokens travel in cleartext."
                    ),
                    impact=(
                        "Any network position between the user and the server — a "
                        "coffee-shop Wi-Fi, a corporate proxy, a compromised router — "
                        "can read and modify the traffic, including session cookies "
                        "and anything typed into forms."
                    ),
                    remediation=(
                        "Serve the site over HTTPS with a valid certificate, redirect "
                        "HTTP to HTTPS, and enable HSTS once the deployment is "
                        "stable."
                    ),
                    cwe="CWE-319",
                    owasp="A02:2021 - Cryptographic Failures",
                    cvss=5.9,
                    evidence=[
                        Evidence(
                            kind=EvidenceKind.MATCH,
                            summary=f"Scanned URL scheme: {parsed.scheme}",
                        )
                    ],
                    source=self.name,
                    tags=["tls", "transport"],
                )
            )
            return found

        response = await context.fetch(context.base_url)
        if not response.ok:
            return found

        if "strict-transport-security" not in response.headers:
            found.append(
                finding(
                    title="HSTS is not enabled",
                    severity=Severity.LOW,
                    category=self.category,
                    confidence=Confidence.CONFIRMED,
                    context=context,
                    description=(
                        "The HTTPS response does not set Strict-Transport-Security, so "
                        "the browser is allowed to make a first plain-HTTP request."
                    ),
                    impact=(
                        "That first request is a downgrade opportunity: an attacker on "
                        "the network strips TLS and captures the cookie that the "
                        "browser would otherwise have refused to send."
                    ),
                    remediation=(
                        "Add 'Strict-Transport-Security: max-age=31536000; includeSubDomains' "
                        "once every subdomain is confirmed to be HTTPS-ready."
                    ),
                    cwe="CWE-319",
                    owasp="A02:2021 - Cryptographic Failures",
                    evidence=[
                        Evidence(
                            kind=EvidenceKind.RESPONSE,
                            summary="Strict-Transport-Security absent from the HTTPS response",
                            request=response.request_line(),
                            response_status=response.status,
                        )
                    ],
                    source=self.name,
                    tags=["tls", "hsts"],
                )
            )

        options = response.header("strict-transport-security", "")
        if options and "max-age" not in options.lower():
            found.append(
                finding(
                    title="HSTS max-age is missing or too short",
                    severity=Severity.INFO,
                    category=self.category,
                    confidence=Confidence.CONFIRMED,
                    context=context,
                    description=(
                        f"Strict-Transport-Security is set to '{options}' without a "
                        "meaningful max-age, so the policy is forgotten quickly."
                    ),
                    impact=(
                        "A short max-age means the protection lapses between visits, "
                        "reopening the downgrade window."
                    ),
                    remediation=(
                        "Use 'max-age=31536000; includeSubDomains' and preload the "
                        "domain once the deployment is stable."
                    ),
                    cwe="CWE-319",
                    owasp="A02:2021 - Cryptographic Failures",
                    evidence=[
                        Evidence(
                            kind=EvidenceKind.RESPONSE,
                            summary=f"Strict-Transport-Security: {options}",
                            request=response.request_line(),
                            response_status=response.status,
                        )
                    ],
                    source=self.name,
                    tags=["tls", "hsts"],
                )
            )
        return found
