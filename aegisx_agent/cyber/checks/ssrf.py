"""SSRF checks — server-side request forgery.

An SSRF is a vulnerability in the *server*, not in the browser: the
application takes a URL from the user and fetches it itself. The
attacker controls where the server goes, which turns the server into
their proxy into the network the browser cannot reach.

Detecting it without a callback server is the interesting problem. The
check uses four probes, each chosen because it is *read-only* and
because its failure mode is unambiguous:

1. **Loopback.** ``http://127.0.0.1/`` — a refused connection proves the
   server dialled out, because the client never sees a socket error.
2. **Reserved TLD.** ``http://<marker>.invalid/`` — RFC 2606 reserves
   ``.invalid`` and it never resolves, so no third party is contacted.
   A server error that names the marker proves the server resolved and
   dialled a host the client supplied.
3. **File scheme.** ``file:///etc/hostname`` — proves the server honours
   non-HTTP schemes. The file read is a read of a harmless system file;
   nothing is written and nothing is deleted.
4. **Cloud metadata.** ``http://169.254.169.254/latest/meta-data/`` —
   proves reachability of the cloud metadata service. The response is
   redacted before it becomes evidence, because the metadata service
   hands out credentials and a report is not the place for them.

Every probe is differential: the response to a probe is compared with
the response to a benign value at the same parameter, so a page that
always errors is not mistaken for an SSRF.
"""

from __future__ import annotations

import random
import re
import string
from dataclasses import dataclass
from typing import TYPE_CHECKING
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

if TYPE_CHECKING:  # avoids a runtime import cycle
    from aegisx_agent.cyber.prober import Response

#: Parameter names that usually carry a URL. Matching the name is only
#: a *candidate* filter — every candidate is still probed and confirmed
#: differentially, so a parameter called "url" that is really a slug
#: costs one request and produces no finding.
URL_PARAM_NAMES: frozenset[str] = frozenset(
    {
        "url",
        "uri",
        "link",
        "href",
        "src",
        "source",
        "dest",
        "destination",
        "redirect",
        "redirect_uri",
        "redirect_url",
        "return",
        "return_url",
        "returnto",
        "next",
        "continue",
        "target",
        "path",
        "file",
        "filename",
        "filepath",
        "folder",
        "domain",
        "host",
        "hostname",
        "server",
        "site",
        "page",
        "view",
        "load",
        "feed",
        "image",
        "img",
        "avatar",
        "proxy",
        "callback",
        "callback_url",
        "webhook",
        "webhook_url",
        "endpoint",
        "address",
        "addr",
        "port",
        "connect",
        "outbound",
        "ping",
        "check",
        "validate",
        "preview",
        "import",
        "download",
    }
)

#: Signatures that only appear when the *server* opened the socket.
SERVER_FETCH_SIGNATURES: tuple[tuple[str, str], ...] = (
    (r"connection refused", "connection refused"),
    (r"econnrefused", "connection refused"),
    (r"connection reset by peer", "connection reset"),
    (r"no route to host", "no route to host"),
    (r"getaddrinfo\s*enoent", "DNS resolution failed"),
    (r"dns lookup failed", "DNS resolution failed"),
    (r"could not resolve host", "DNS resolution failed"),
    (r"unknown host", "DNS resolution failed"),
    (r"name or service not known", "DNS resolution failed"),
    (r"failed to connect to", "connection failed"),
    (r"unable to connect to", "connection failed"),
    (r"invalid url", "invalid URL rejected"),
    (r"unsupported protocol scheme", "unsupported URL scheme"),
    (r"unknown url type", "unsupported URL scheme"),
    (r"no protocol supplied", "unsupported URL scheme"),
)

#: Content that proves a local file was read through a ``file://`` URL.
FILE_CONTENT_SIGNATURES: tuple[tuple[str, str], ...] = (
    (r"\broot:[x*!]?:0:0:", "/etc/passwd was read"),
    (r"\blocalhost\b", "/etc/hostname was read"),
    (r"\blinux\b", "a system file was read"),
)

#: Keys the cloud metadata service exposes. Their *presence* proves
#: reachability; their *values* are redacted.
METADATA_MARKERS: tuple[str, ...] = (
    "latest/meta-data",
    "instance-identity",
    "meta-data/instance-id",
    "computeMetadata",
    "x-aws-ec2-metadata-token",
)

#: Long opaque strings, which is what a credential looks like.
_SECRET_LIKE = re.compile(r"[A-Za-z0-9/+_-]{24,}={0,2}")


def _marker(prefix: str = "aegisx") -> str:
    """A token unlikely to appear naturally in a response."""
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    return f"{prefix}{suffix}"


def _redact(text: str) -> str:
    """Blank out anything that looks like a credential."""
    return _SECRET_LIKE.sub("[redacted]", text)


def _url_candidates(endpoints: list[str], limit: int = 8) -> list[tuple[str, str]]:
    """Endpoints whose query string contains a URL-shaped parameter."""
    candidates: list[tuple[str, str]] = []
    for endpoint in endpoints:
        parsed = urlparse(endpoint)
        if not parsed.query:
            continue
        for name, values in parse_qs(parsed.query, keep_blank_values=True).items():
            if name.lower() not in URL_PARAM_NAMES:
                continue
            value = values[0] if values else ""
            if "://" in value or value.startswith("//"):
                candidates.append((endpoint, name))
                break
    return candidates[:limit]


def _with_param(url: str, name: str, value: str) -> str:
    """Return the URL with one query parameter replaced."""
    parsed = urlparse(url)
    pairs = parse_qs(parsed.query, keep_blank_values=True)
    pairs[name] = [value]
    query = urlencode(pairs, doseq=True)
    return parsed._replace(query=query).geturl()


def _signature(body: str) -> tuple[str, str] | None:
    """The first server-fetch signature present in a body."""
    lowered = body.lower()
    for pattern, label in SERVER_FETCH_SIGNATURES:
        if re.search(pattern, lowered):
            return pattern, label
    return None


def _file_signature(body: str) -> str | None:
    """Whether the body contains content that only a local file read yields."""
    for pattern, label in FILE_CONTENT_SIGNATURES:
        if re.search(pattern, body, re.IGNORECASE):
            return label
    return None


@dataclass
class SSRFCheck:
    """Detect server-side request forgery through differential probing.

    The check only reports when the *server* shows evidence of its own
    outbound attempt: a connection error naming the injected address, a
    DNS error naming the injected host, or content that could only have
    come from a local file.
    """

    name: str = "ssrf"
    category: Category = Category.INJECTION
    cost: str = "deep"

    async def run(self, context: CheckContext) -> list[Finding]:
        found: list[Finding] = []
        for endpoint, param in _url_candidates(context.endpoints):
            found.extend(await self._probe_endpoint(context, endpoint, param))
        return found

    async def _probe_endpoint(
        self, context: CheckContext, endpoint: str, param: str
    ) -> list[Finding]:
        """Probe one URL-shaped parameter and describe what came back."""
        found: list[Finding] = []

        baseline = await context.fetch(_with_param(endpoint, param, "https://example.com/"))
        if baseline.error:
            return found

        probes: list[tuple[str, str, str]] = [
            (
                "loopback",
                "http://127.0.0.1/",
                "The server attempted to connect to its own loopback interface.",
            ),
            (
                "link-local",
                "http://169.254.169.254/latest/meta-data/",
                "The server attempted to reach the cloud metadata service.",
            ),
            (
                "reserved-tld",
                f"http://{_marker('canary')}.invalid/",
                "The server resolved and dialled a host the client supplied.",
            ),
            (
                "file-scheme",
                "file:///etc/hostname",
                "The server honoured a non-HTTP scheme and read a local file.",
            ),
        ]

        for kind, payload, description in probes:
            response = await context.fetch(_with_param(endpoint, param, payload))
            if response.error:
                continue

            issue = self._classify(kind, response)
            if issue is None:
                continue

            severity, label, detail = issue
            found.append(
                finding(
                    title=f"Server-side request forgery in parameter '{param}' ({label})",
                    severity=severity,
                    category=self.category,
                    confidence=Confidence.CONFIRMED,
                    context=context,
                    endpoint=endpoint,
                    description=(
                        f"The parameter '{param}' on {endpoint} is fetched by the server. "
                        f"{description} The response differs from the one produced by a "
                        "benign URL in the same parameter."
                    ),
                    impact=(
                        "An attacker chooses where the server connects. From there the "
                        "server can reach internal services, cloud metadata endpoints, "
                        "and hosts that only the server's network can see — and the "
                        "response is usually returned to the attacker."
                    ),
                    remediation=(
                        "Allow-list the destinations this parameter may point at, and "
                        "reject everything else. Resolve the host and check the "
                        "resulting address against private, loopback, and link-local "
                        "ranges *after* resolution, on every request — not only on "
                        "the URL the client sent."
                    ),
                    cwe="CWE-918",
                    owasp="A10:2021 - Server-Side Request Forgery",
                    cvss=8.6,
                    references=[
                        "https://owasp.org/www-community/attacks/Server_Side_Request_Forgery",
                        "https://portswigger.net/web-security/ssrf",
                    ],
                    evidence=[
                        Evidence(
                            kind=EvidenceKind.REQUEST,
                            summary=f"Payload sent in parameter '{param}': {payload}",
                            request=(f"{response.method} {response.url}\n{param}={payload}"),
                            response_status=response.status,
                        ),
                        Evidence(
                            kind=EvidenceKind.RESPONSE,
                            summary=detail,
                            request=response.request_line(),
                            response_status=response.status,
                            snippet=_redact(response.body[:400]),
                        ),
                    ],
                    source=self.name,
                    tags=["ssrf", kind, param],
                )
            )
        return found

    def _classify(self, kind: str, response: Response) -> tuple[Severity, str, str] | None:
        """Decide what a probe's response proves, if anything."""
        if kind == "file-scheme":
            label = _file_signature(response.body)
            if label is None:
                return None
            return (
                Severity.CRITICAL,
                "local file read",
                f"Response contains local file content: {label}.",
            )

        if kind == "link-local":
            lowered = response.body.lower()
            if not any(marker.lower() in lowered for marker in METADATA_MARKERS):
                return None
            return (
                Severity.CRITICAL,
                "cloud metadata reachable",
                "The response contains the cloud metadata service's own markers. "
                "The values in it have been redacted, because that service hands "
                "out credentials.",
            )

        signature = _signature(response.body)
        if signature is None:
            return None
        _pattern, label = signature
        if kind == "loopback":
            severity = Severity.HIGH
        elif kind == "reserved-tld":
            severity = Severity.HIGH
        else:
            severity = Severity.MEDIUM
        return (severity, kind, f"The server's own error surfaced: {label}.")
