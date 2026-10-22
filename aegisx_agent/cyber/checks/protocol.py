"""HTTP protocol checks — response splitting and host header injection.

These two flaws live in the layer between the application and the
client, so the probes have to go through the server rather than around
it.

- **CRLF injection.** When user input reaches an HTTP header without
  sanitisation, a ``\\r\\n`` in the value ends the header and starts a
  new one. The proof is a header that only exists because the server
  injected it.
- **Host header injection.** When the application builds absolute URLs
  from the ``Host`` header, the attacker chooses where the link points.
  The proof is the attacker's host appearing in a redirect target, in a
  generated link, or in a cache key.

Both probes are read-only. The host header probe uses ``example.com``,
which RFC 2606 reserves for documentation and which resolves nowhere
useful, and the CRLF probe injects a header name rather than a
``Location`` — no victim is redirected and no cache is poisoned.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from aegisx_agent.cyber.checks import CheckContext, finding
from aegisx_agent.cyber.models import (
    Category,
    Confidence,
    Evidence,
    EvidenceKind,
    Finding,
    Severity,
)

#: The host the probes pretend to be. RFC 2606 reserves example.com
#: for documentation, so it never points at somebody's service.
PROBE_HOST = "aegisx-host-probe.example.com"

#: The header name the CRLF probe tries to smuggle in.
PROBE_HEADER = "x-aegisx-injected"

#: The marker value that accompanies it.
PROBE_MARKER = "aegisx-crlf-marker"


def _with_param(url: str, name: str, value: str) -> str:
    """Return the URL with one query parameter replaced."""
    parsed = urlparse(url)
    pairs = parse_qs(parsed.query, keep_blank_values=True)
    pairs[name] = [value]
    query = urlencode(pairs, doseq=True)
    return parsed._replace(query=query).geturl()


def _query_endpoints(endpoints: list[str], limit: int = 6) -> list[str]:
    """Endpoints that carry query parameters."""
    candidates = [endpoint for endpoint in endpoints if urlparse(endpoint).query]
    return candidates[:limit]


@dataclass
class CRLFInjectionCheck:
    """Detect HTTP response splitting through unsanitised headers.

    The payload is percent-encoded so it survives the URL intact and is
    decoded by the *application*, which is exactly where the flaw has
    to be for the test to mean anything.
    """

    name: str = "crlf-injection"
    category: Category = Category.INJECTION
    cost: str = "deep"

    async def run(self, context: CheckContext) -> list[Finding]:
        found: list[Finding] = []
        for endpoint in _query_endpoints(context.endpoints):
            parsed = urlparse(endpoint)
            pairs = parse_qs(parsed.query, keep_blank_values=True)
            if not pairs:
                continue
            name = next(iter(pairs))
            original = pairs[name][0] if pairs[name] else ""

            payload = f"{original}%0d%0a{PROBE_HEADER}: {PROBE_MARKER}"
            response = await context.fetch(_with_param(endpoint, name, payload))
            if response.error:
                continue

            smuggled = response.header(PROBE_HEADER)
            reflected = PROBE_MARKER in response.body
            if not smuggled and not reflected:
                continue

            found.append(
                finding(
                    title=f"HTTP header injection (CRLF) in parameter '{name}'",
                    severity=Severity.HIGH,
                    category=self.category,
                    confidence=Confidence.CONFIRMED,
                    context=context,
                    endpoint=endpoint,
                    description=(
                        f"The parameter '{name}' on {endpoint} reaches an HTTP header "
                        "without the carriage return and line feed being stripped. "
                        + (
                            f"The server emitted a '{PROBE_HEADER}' header that the "
                            "client never sent."
                            if smuggled
                            else "The injected line break appears in the response body."
                        )
                    ),
                    impact=(
                        "A response split this way lets the attacker set headers and "
                        "a body of their own in a response the victim's browser "
                        "treats as the site's own — which is how cache poisoning "
                        "and cross-site scripting get a trusted origin."
                    ),
                    remediation=(
                        "Strip CR, LF, and NUL from any value that reaches a header, "
                        "or reject the request outright. Encode the value where the "
                        "protocol allows it, and never let a client-supplied value "
                        "terminate a header."
                    ),
                    cwe="CWE-113",
                    owasp="A03:2021 - Injection",
                    cvss=8.1,
                    references=["https://cwe.mitre.org/data/definitions/113.html"],
                    evidence=[
                        Evidence(
                            kind=EvidenceKind.REQUEST,
                            summary="CRLF payload sent in the query parameter",
                            request=response.request_line(),
                            response_status=response.status,
                        ),
                        Evidence(
                            kind=EvidenceKind.RESPONSE,
                            summary=(
                                f"Server emitted the injected header {PROBE_HEADER!r}"
                                if smuggled
                                else "Injected line break reflected in the body"
                            ),
                            request=response.request_line(),
                            response_status=response.status,
                            snippet=response.body[:300],
                        ),
                    ],
                    source=self.name,
                    tags=["crlf", "response-splitting", "injection", name],
                )
            )
            break
        return found


@dataclass
class HostHeaderCheck:
    """Detect host header injection into generated links and redirects.

    The probe changes the ``Host`` header and sends
    ``X-Forwarded-Host``. A finding requires the attacker's host to come
    back in a redirect target, in the body, or in a header that a cache
    would key on.
    """

    name: str = "host-header-injection"
    category: Category = Category.INJECTION
    cost: str = "deep"

    async def run(self, context: CheckContext) -> list[Finding]:
        found: list[Finding] = []
        probes: list[tuple[str, dict[str, str]]] = [
            ("Host", {"Host": PROBE_HOST}),
            ("X-Forwarded-Host", {"X-Forwarded-Host": PROBE_HOST}),
        ]

        for endpoint in [context.base_url, *_query_endpoints(context.endpoints, limit=3)]:
            for header_name, headers in probes:
                response = await context.fetch(endpoint, headers=headers)
                if response.error:
                    continue
                location = response.header("location")
                reflected_in = self._reflection_point(response, location)
                if reflected_in is None:
                    continue

                found.append(
                    finding(
                        title="Host header injection into a generated link",
                        severity=Severity.HIGH,
                        category=self.category,
                        confidence=Confidence.CONFIRMED,
                        context=context,
                        endpoint=endpoint,
                        description=(
                            f"The {header_name} header is used to build the link the "
                            f"server returns. Sending '{PROBE_HOST}' in that header "
                            f"put it into {reflected_in} of the response, so the "
                            "client is directed to a host the attacker chose."
                        ),
                        impact=(
                            "Every absolute link the application generates becomes "
                            "attacker-controllable. In a password-reset or "
                            "payment-callback flow, that is enough to capture a "
                            "credential that the user was told to send to the real "
                            "site."
                        ),
                        remediation=(
                            "Build absolute URLs from a configured base URL, not "
                            "from the request's Host header. If a forwarded host "
                            "must be honoured, allow-list it against the hosts the "
                            "service actually serves."
                        ),
                        cwe="CWE-644",
                        owasp="A03:2021 - Injection",
                        cvss=8.1,
                        references=["https://cwe.mitre.org/data/definitions/644.html"],
                        evidence=[
                            Evidence(
                                kind=EvidenceKind.REQUEST,
                                summary=f"{header_name} header replaced with the probe host",
                                request=(f"GET {endpoint}\n{header_name}: {PROBE_HOST}"),
                                response_status=response.status,
                            ),
                            Evidence(
                                kind=EvidenceKind.RESPONSE,
                                summary=f"Probe host reflected in {reflected_in}",
                                request=response.request_line(),
                                response_status=response.status,
                                snippet=response.body[:300],
                            ),
                        ],
                        source=self.name,
                        tags=["host-header", "poisoning", "redirect"],
                    )
                )
                return found
        return found

    @staticmethod
    def _reflection_point(response: Any, location: str) -> str | None:
        """Where the probe host showed up in the response, if it did."""
        if PROBE_HOST in location:
            return "the Location header"
        if PROBE_HOST in response.body:
            return "the response body"
        for name, value in response.headers.items():
            if PROBE_HOST in value and name.lower() in {"location", "content-location", "link"}:
                return f"the {name} header"
        return None
