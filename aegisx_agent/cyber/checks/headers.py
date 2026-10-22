"""Security header and cookie hygiene checks.

These are the cheapest checks in the suite and the ones a target is most
likely to fail. They are also the ones most likely to be dismissed as
"just hardening" — so each finding states the concrete attack it enables
(clickjacking, credential theft, MIME sniffing) rather than only naming
the missing header.
"""

from __future__ import annotations

from dataclasses import dataclass

from aegisx_agent.cyber.checks import CheckContext, finding
from aegisx_agent.cyber.models import (
    Category,
    Confidence,
    Evidence,
    EvidenceKind,
    Finding,
    Severity,
)

#: header -> (why it matters, what an attacker gains without it)
SECURITY_HEADERS: dict[str, tuple[str, str, Severity, str]] = {
    "content-security-policy": (
        "Content-Security-Policy",
        "A missing CSP means an injected script or an inline handler runs with the "
        "victim's session. It is the single control that turns stored XSS from a "
        "session hijack into a non-event.",
        Severity.MEDIUM,
        "CWE-1021",
    ),
    "strict-transport-security": (
        "Strict-Transport-Security",
        "Without HSTS, the first request to the site can be intercepted on a hostile "
        "network and the session cookie is handed over in cleartext.",
        Severity.MEDIUM,
        "CWE-319",
    ),
    "x-content-type-options": (
        "X-Content-Type-Options",
        "Without nosniff the browser guesses a content type, so an uploaded file the "
        "server meant to serve as data can be executed as script.",
        Severity.LOW,
        "CWE-16",
    ),
    "x-frame-options": (
        "X-Frame-Options",
        "Without it, the page can be framed by an attacker and the victim can be "
        "tricked into clicking a hidden control — clickjacking against an action "
        "the site performs on their behalf.",
        Severity.MEDIUM,
        "CWE-1021",
    ),
    "referrer-policy": (
        "Referrer-Policy",
        "Without it, the full URL of every page — including tokens in the path and "
        "query — is leaked to every third party the page links to.",
        Severity.LOW,
        "CWE-200",
    ),
    "permissions-policy": (
        "Permissions-Policy",
        "Without it, a compromised or injected script can turn on the camera, "
        "microphone, or geolocation of the victim.",
        Severity.LOW,
        "CWE-16",
    ),
}

#: Header names whose *presence* is the finding.
DANGEROUS_HEADERS: dict[str, tuple[str, str, Severity]] = {
    "server": (
        "Server header discloses the server software and version",
        "The exact version narrows the search for known vulnerabilities from the "
        "entire CVE space to a handful of relevant entries.",
        Severity.INFO,
    ),
    "x-powered-by": (
        "X-Powered-By discloses the application framework and version",
        "A framework disclosure is a free fingerprint that saves an attacker an "
        "entire recon phase.",
        Severity.INFO,
    ),
}

#: Cookies should carry these attributes.
COOKIE_ATTRIBUTES: dict[str, tuple[str, Severity]] = {
    "secure": (
        "Without Secure, the cookie is sent over plain HTTP, so any network "
        "observer between the user and the server harvests it.",
        Severity.MEDIUM,
    ),
    "httponly": (
        "Without HttpOnly, JavaScript on the page can read the session cookie — "
        "one successful XSS becomes a full session takeover.",
        Severity.MEDIUM,
    ),
    "samesite": (
        "Without SameSite, the cookie is attached to cross-site requests, which "
        "enables CSRF against every state-changing endpoint.",
        Severity.LOW,
    ),
}


@dataclass
class HeaderCheck:
    """Report missing or leaky security headers."""

    name: str = "security-headers"
    category: Category = Category.HEADERS
    cost: str = "cheap"

    async def run(self, context: CheckContext) -> list[Finding]:
        response = await context.fetch(context.base_url)
        if not response.ok:
            return []

        found: list[Finding] = []
        present = set(response.headers)

        for header, (label, why, severity, cwe) in SECURITY_HEADERS.items():
            if header in present:
                continue
            # CSP is satisfied by any equivalent modern header.
            if header == "content-security-policy" and (
                "content-security-policy-report-only" in present
            ):
                continue
            # HSTS is only meaningful over TLS.
            if header == "strict-transport-security" and not context.base_url.startswith(
                "https://"
            ):
                continue
            found.append(
                finding(
                    title=f"Missing security header: {label}",
                    severity=severity,
                    category=self.category,
                    confidence=Confidence.CONFIRMED,
                    context=context,
                    description=f"The response does not set {label}.",
                    impact=why,
                    remediation=f"Set {label} on every response from this origin.",
                    cwe=cwe,
                    owasp="A05:2021 - Security Misconfiguration",
                    evidence=[
                        Evidence(
                            kind=EvidenceKind.RESPONSE,
                            summary=f"{label} absent from the response headers",
                            request=response.request_line(),
                            response_status=response.status,
                        )
                    ],
                    source=self.name,
                    tags=["headers", label.lower()],
                )
            )

        for header, (label, why, severity) in DANGEROUS_HEADERS.items():
            value = response.header(header)
            if not value:
                continue
            found.append(
                finding(
                    title=label,
                    severity=severity,
                    category=self.category,
                    confidence=Confidence.CONFIRMED,
                    context=context,
                    description=f"The response discloses {header}: {value}.",
                    impact=why,
                    remediation=f"Remove or suppress the {header} header at the edge.",
                    owasp="A05:2021 - Security Misconfiguration",
                    evidence=[
                        Evidence(
                            kind=EvidenceKind.RESPONSE,
                            summary=f"{header}: {value}",
                            request=response.request_line(),
                            response_status=response.status,
                        )
                    ],
                    source=self.name,
                    tags=["headers", "information-disclosure"],
                )
            )
        return found


@dataclass
class CookieCheck:
    """Check cookies for missing Secure / HttpOnly / SameSite attributes."""

    name: str = "cookie-flags"
    category: Category = Category.COOKIE
    cost: str = "cheap"

    async def run(self, context: CheckContext) -> list[Finding]:
        response = await context.fetch(context.base_url)
        if not response.ok:
            return []

        found: list[Finding] = []
        for raw in response.header_values("set-cookie"):
            cookie_name, _, attributes = raw.partition("=")
            name = cookie_name.strip() or "cookie"
            lowered = attributes.lower()
            missing = [flag for flag in COOKIE_ATTRIBUTES if flag not in lowered]
            if not missing:
                continue

            # The finding takes the severity of the worst missing attribute,
            # and quotes the impact of that same attribute.
            worst_flag = max(missing, key=lambda flag: COOKIE_ATTRIBUTES[flag][1].weight)
            severity = COOKIE_ATTRIBUTES[worst_flag][1]
            worst = COOKIE_ATTRIBUTES[worst_flag][1]
            described = ", ".join(missing)
            found.append(
                finding(
                    title=f"Cookie '{name}' is missing {described}",
                    severity=severity,
                    category=self.category,
                    confidence=Confidence.CONFIRMED,
                    context=context,
                    description=(f"The cookie '{name}' is set without {described}."),
                    impact=worst,
                    remediation=(
                        f"Set the cookie with {described}, e.g. "
                        f"'Set-Cookie: {name}=...; Secure; HttpOnly; SameSite=Lax'."
                    ),
                    cwe="CWE-614",
                    owasp="A07:2021 - Identification and Authentication Failures",
                    evidence=[
                        Evidence(
                            kind=EvidenceKind.RESPONSE,
                            summary=f"Set-Cookie: {raw}",
                            request=response.request_line(),
                            response_status=response.status,
                        )
                    ],
                    source=self.name,
                    tags=["cookie", *missing],
                )
            )
        return found
