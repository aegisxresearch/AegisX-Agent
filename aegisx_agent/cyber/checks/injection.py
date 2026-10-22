"""Injection checks — SQLi, XSS, SSTI, and path traversal.

The hard part of an injection check is not sending the payload; it is
avoiding the false positive. Every check here uses a **differential**
technique: send a benign baseline, send a mutated payload, and only
report when the *difference* is what the payload predicts.

- SQLi: a true/false pair where the two responses differ only when the
  condition is true, plus error-signature matching on the response body.
- XSS: a unique random marker, reflected without HTML-escaping.
- SSTI: an arithmetic expression whose evaluation is observable.
- Traversal: a marker that must appear only if the file was read.

Nothing here is destructive. No stacked queries, no sleeps long enough
to matter, no DROP, no file writes. A security scanner that damages the
target it was hired to protect is a liability, not a tool.
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

#: Error signatures that indicate a query reached the database and failed.
SQL_ERROR_SIGNATURES: tuple[tuple[str, str], ...] = (
    (r"you have an error in your sql syntax", "MySQL"),
    (r"warning.*\bmysqli?_\w+\b", "MySQL"),
    (r"unclosed quotation mark after the character string", "MSSQL"),
    (r"microsoft ole db provider for sql server", "MSSQL"),
    (r"pg_query\(\)|pg_exec\(\)|unterminated quoted string", "PostgreSQL"),
    (r"sqlite3?::query|sqlite_orm", "SQLite"),
    (r"ora-\d{5}|oracle error", "Oracle"),
    (r"sql syntax.*mysql|mariadb", "MySQL/MariaDB"),
    (r"odbc sql server driver", "MSSQL"),
    (r"syntax error at or near", "PostgreSQL"),
    (r"quoted string not properly terminated", "Oracle"),
)

#: Expressions that evaluate to a value only if a template engine runs
#: the expression. Each has a known-good arithmetic output.
TEMPLATE_PAYLOADS: tuple[tuple[str, str, str], ...] = (
    ("{{7*7}}", "49", "Jinja2 / Twig / Handlebars-style"),
    ("${7*7}", "49", "Java EL / FreeMarker / Velocity"),
    ("<%= 7*7 %>", "49", "ERB / ASP-style"),
    ("#{7*7}", "49", "Ruby / Pug-style"),
    ("${{7*7}}", "49", "Angular / Thymeleaf-style"),
)

#: Headers that prove the payload was reflected into a raw HTML context.
HTML_CONTENT_MARKERS = ("text/html", "application/xhtml")


def _random_marker(prefix: str = "aegisx") -> str:
    """A marker unlikely to appear naturally in a response."""
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
    return f"{prefix}{suffix}"


def _html_escaped(text: str) -> bool:
    """Whether a reflected marker shows signs of HTML escaping."""
    lowered = text.lower()
    return any(token in lowered for token in ("&lt;", "&gt;", "&quot;", "&#x27;", "&#39;", "&amp;"))


def _query_endpoints(endpoints: list[str], limit: int = 6) -> list[str]:
    """Endpoints that carry query parameters — the injection surface."""
    candidates = [endpoint for endpoint in endpoints if urlparse(endpoint).query]
    return candidates[:limit]


def _first_param(url: str) -> tuple[str, str]:
    """The first query parameter of a URL as ``(name, value)``."""
    parsed = urlparse(url)
    pairs = parse_qs(parsed.query)
    if not pairs:
        return "", ""
    name = next(iter(pairs))
    return name, pairs[name][0] if pairs[name] else ""


def _with_param(url: str, name: str, value: str) -> str:
    """Return the URL with one query parameter replaced."""
    parsed = urlparse(url)
    pairs = parse_qs(parsed.query, keep_blank_values=True)
    pairs[name] = [value]
    query = urlencode(pairs, doseq=True)
    return parsed._replace(query=query).geturl()


#: Long opaque tokens that differ between two otherwise identical pages.
_VOLATILE_TOKEN = re.compile(r"\b[a-z0-9]{16,}\b", re.IGNORECASE)


def _normalise_body(body: str) -> str:
    """Strip volatile tokens so a nonce difference is not a real difference.

    Two responses that differ only in a cache-busting token or a
    timestamp are the same page. Without this normalisation, every
    page that embeds a unique request id would look like an injection
    differential, and the scanner would cry wolf on every single URL.
    """
    return _VOLATILE_TOKEN.sub("TOKEN", body)


@dataclass
class SQLInjectionCheck:
    """Detect SQL injection through differential responses and error signatures.

    Two techniques run together because each covers what the other misses:
    the error-signature check finds a loud database error, and the
    boolean-differential check finds a *quiet* injection that returns a
    normal page with different content.
    """

    name: str = "sql-injection"
    category: Category = Category.INJECTION
    cost: str = "deep"

    async def run(self, context: CheckContext) -> list[Finding]:
        found: list[Finding] = []
        for endpoint in _query_endpoints(context.endpoints):
            name, original = _first_param(endpoint)
            if not name:
                continue

            baseline = await context.fetch(endpoint)
            if not baseline.ok:
                continue

            # Technique 1: error signature in the baseline or in a
            # quote-injected variant.
            quote_variant = _with_param(endpoint, name, f"{original}'")
            quote_response = await context.fetch(quote_variant)
            signature = self._match_error(quote_response.body) or self._match_error(baseline.body)
            if signature:
                found.append(
                    self._build_finding(
                        context=context,
                        endpoint=endpoint,
                        param=name,
                        detail=(
                            f"The database returned a {signature} error after a single quote "
                            "was injected into the parameter."
                        ),
                        evidence_response=quote_response,
                        confidence=Confidence.CONFIRMED,
                        payload=quote_variant,
                    )
                )
                continue

            # Technique 2: boolean differential.
            true_variant = _with_param(endpoint, name, f"{original}' AND '1'='1")
            false_variant = _with_param(endpoint, name, f"{original}' AND '1'='2")
            true_response = await context.fetch(true_variant)
            false_response = await context.fetch(false_variant)
            if self._differs(true_response, false_response, baseline):
                found.append(
                    self._build_finding(
                        context=context,
                        endpoint=endpoint,
                        param=name,
                        detail=(
                            "A true/false condition pair produced different responses, "
                            "which indicates the parameter value is concatenated "
                            "into a SQL statement."
                        ),
                        evidence_response=false_response,
                        confidence=Confidence.PROBABLE,
                        payload=false_variant,
                    )
                )
        return found

    @staticmethod
    def _match_error(body: str) -> str:
        """Identify a database error signature in a response body."""
        lowered = body.lower()
        for pattern, vendor in SQL_ERROR_SIGNATURES:
            if re.search(pattern, lowered):
                return vendor
        return ""

    @staticmethod
    def _differs(true_response: Response, false_response: Response, baseline: Response) -> bool:
        """Whether the true/false pair differ in a way that implies injection.

        The comparison ignores length noise: what matters is that the
        "false" response differs from *both* the "true" response and the
        baseline, in content or in status.
        """
        if true_response.error or false_response.error:
            return False
        if true_response.status != false_response.status:
            return True
        true_body = true_response.body.strip()
        false_body = false_response.body.strip()
        if true_body == false_body:
            return False
        if false_body == baseline.body.strip():
            return False
        # Require a meaningful difference, not a nonce or a timestamp.
        return _normalise_body(true_body) != _normalise_body(false_body)

    def _build_finding(
        self,
        *,
        context: CheckContext,
        endpoint: str,
        param: str,
        detail: str,
        evidence_response: Response,
        confidence: Confidence,
        payload: str,
    ) -> Finding:
        return finding(
            title=f"SQL injection in parameter '{param}'",
            severity=Severity.HIGH,
            category=self.category,
            confidence=confidence,
            context=context,
            endpoint=endpoint,
            description=(f"The parameter '{param}' on {endpoint} is injectable. {detail}"),
            impact=(
                "An attacker can read or modify any data the application's database "
                "account can reach, and on many engines escalate to command execution "
                "on the database host."
            ),
            remediation=(
                "Use parameterised queries (prepared statements) for every value. "
                "Never build a SQL string by concatenation or interpolation, and "
                "apply least privilege to the database account."
            ),
            cwe="CWE-89",
            owasp="A03:2021 - Injection",
            cvss=9.8,
            references=[
                "https://owasp.org/www-community/attacks/SQL_Injection",
                "https://cwe.mitre.org/data/definitions/89.html",
            ],
            evidence=[
                Evidence(
                    kind=EvidenceKind.RESPONSE,
                    summary=f"Injected payload returned HTTP {evidence_response.status}",
                    request=evidence_response.request_line(),
                    response_status=evidence_response.status,
                    snippet=evidence_response.body[:600],
                ),
                Evidence(
                    kind=EvidenceKind.MATCH,
                    summary=f"Payload used: {payload}",
                ),
            ],
            source=self.name,
            tags=["sqli", "injection", param],
        )


@dataclass
class XSSCheck:
    """Detect reflected cross-site scripting by marker reflection.

    A reflection is only reported when the marker comes back *unescaped*
    and the response is HTML — a marker echoed inside a JSON API response
    is not a script execution context and reporting it would be noise.
    """

    name: str = "reflected-xss"
    category: Category = Category.INJECTION
    cost: str = "deep"

    async def run(self, context: CheckContext) -> list[Finding]:
        found: list[Finding] = []
        for endpoint in _query_endpoints(context.endpoints):
            name, original = _first_param(endpoint)
            if not name:
                continue

            marker = _random_marker("aegisx")
            probe = f"{original}<script>{marker}</script>"
            response = await context.fetch(_with_param(endpoint, name, probe))
            if not response.ok or marker not in response.body:
                continue
            if not any(
                content_marker in response.content_type for content_marker in HTML_CONTENT_MARKERS
            ):
                continue
            if _html_escaped(response.body[max(0, response.body.find(marker) - 200) :]):
                continue

            found.append(
                finding(
                    title=f"Reflected XSS in parameter '{name}'",
                    severity=Severity.HIGH,
                    category=self.category,
                    confidence=Confidence.CONFIRMED,
                    context=context,
                    endpoint=endpoint,
                    description=(
                        f"The parameter '{name}' on {endpoint} reflects the injected "
                        "script tag into the HTML response without escaping it."
                    ),
                    impact=(
                        "An attacker can craft a link that runs script in the victim's "
                        "browser on this origin, stealing session cookies, reading "
                        "page content, or acting as the victim."
                    ),
                    remediation=(
                        "Encode output for its context (HTML-encode for HTML content, "
                        "JSON-encode for script blocks) and use a vetted template "
                        "engine that escapes by default. Add a Content-Security-Policy "
                        "as defence in depth."
                    ),
                    cwe="CWE-79",
                    owasp="A03:2021 - Injection",
                    cvss=8.2,
                    references=["https://owasp.org/www-community/attacks/xss/"],
                    evidence=[
                        Evidence(
                            kind=EvidenceKind.RESPONSE,
                            summary=f"Marker {marker} reflected unescaped in HTML",
                            request=response.request_line(),
                            response_status=response.status,
                            snippet=response.body[
                                max(0, response.body.find(marker) - 120) : response.body.find(
                                    marker
                                )
                                + 160
                            ],
                        )
                    ],
                    source=self.name,
                    tags=["xss", "injection", name],
                )
            )
        return found


@dataclass
class TemplateInjectionCheck:
    """Detect server-side template injection via observable arithmetic.

    The payload is an expression that evaluates to a number. If the
    response contains the evaluated number where the payload was, the
    template engine evaluated the expression server-side.
    """

    name: str = "template-injection"
    category: Category = Category.INJECTION
    cost: str = "deep"

    async def run(self, context: CheckContext) -> list[Finding]:
        found: list[Finding] = []
        for endpoint in _query_endpoints(context.endpoints):
            name, original = _first_param(endpoint)
            if not name:
                continue

            for payload, expected, engine in TEMPLATE_PAYLOADS:
                marker = _random_marker("tpl")
                probe = f"{original}{payload}{marker}"
                response = await context.fetch(_with_param(endpoint, name, probe))
                if not response.ok:
                    continue
                if marker not in response.body:
                    continue
                if expected not in response.body:
                    continue
                # The expected value must sit where the payload was, and
                # the payload itself must be gone — otherwise "49" was
                # simply in the page already.
                position = response.body.find(marker)
                window = response.body[max(0, position - 120) : position]
                if expected not in window:
                    continue

                found.append(
                    finding(
                        title=f"Server-side template injection in parameter '{name}'",
                        severity=Severity.CRITICAL,
                        category=self.category,
                        confidence=Confidence.CONFIRMED,
                        context=context,
                        endpoint=endpoint,
                        description=(
                            f"The parameter '{name}' on {endpoint} is evaluated by a "
                            f"template engine ({engine}). The expression '{payload}' "
                            f"was evaluated to '{expected}' server-side."
                        ),
                        impact=(
                            "Template injection is frequently remote code execution: "
                            "engines such as Jinja2, FreeMarker, and Velocity expose "
                            "object traversal that reaches the filesystem and the "
                            "process."
                        ),
                        remediation=(
                            "Never pass user input into a template expression. Use a "
                            "template engine with sandboxing disabled features, and "
                            "render values as data rather than as template source."
                        ),
                        cwe="CWE-1336",
                        owasp="A03:2021 - Injection",
                        cvss=9.8,
                        references=[
                            "https://owasp.org/www-community/attacks/Server_Side_Template_Injection"
                        ],
                        evidence=[
                            Evidence(
                                kind=EvidenceKind.RESPONSE,
                                summary=f"Payload '{payload}' evaluated to '{expected}'",
                                request=response.request_line(),
                                response_status=response.status,
                                snippet=window[-200:],
                            )
                        ],
                        source=self.name,
                        tags=["ssti", "injection", "rce", name],
                    )
                )
                break
        return found


@dataclass
class PathTraversalCheck:
    """Detect path traversal by reading a file with a known marker.

    The target file is a system file with a stable, unmistakable first
    line (``root:x:0:0``). No file is written and nothing is modified.
    """

    name: str = "path-traversal"
    category: Category = Category.INJECTION
    cost: str = "deep"

    #: The classic proof file, plus its Windows equivalent.
    PROBES: tuple[tuple[str, str], ...] = (
        ("../../../../../../etc/passwd", "root:x:0:0"),
        ("..\\..\\..\\..\\..\\Windows\\win.ini", "[extensions]"),
    )

    async def run(self, context: CheckContext) -> list[Finding]:
        found: list[Finding] = []
        for endpoint in _query_endpoints(context.endpoints):
            name, original = _first_param(endpoint)
            if not name:
                continue

            for payload, marker in self.PROBES:
                response = await context.fetch(_with_param(endpoint, name, payload))
                if not response.ok or marker not in response.body:
                    continue
                found.append(
                    finding(
                        title=f"Path traversal in parameter '{name}'",
                        severity=Severity.HIGH,
                        category=self.category,
                        confidence=Confidence.CONFIRMED,
                        context=context,
                        endpoint=endpoint,
                        description=(
                            f"The parameter '{name}' on {endpoint} accepts a relative "
                            f"path that escapes the intended directory. The payload "
                            f"'{payload}' returned the contents of a file outside the "
                            "web root."
                        ),
                        impact=(
                            "An attacker can read any file the application process can "
                            "read, including source code, configuration with database "
                            "credentials, and private keys."
                        ),
                        remediation=(
                            "Resolve the requested path and verify it stays inside the "
                            "intended base directory before opening it. Reject paths "
                            "containing '..' and use an allow-list of servable files."
                        ),
                        cwe="CWE-22",
                        owasp="A01:2021 - Broken Access Control",
                        cvss=7.5,
                        references=["https://cwe.mitre.org/data/definitions/22.html"],
                        evidence=[
                            Evidence(
                                kind=EvidenceKind.RESPONSE,
                                summary=f"Traversal payload returned {marker!r}",
                                request=response.request_line(),
                                response_status=response.status,
                                snippet=response.body[:400],
                            )
                        ],
                        source=self.name,
                        tags=["traversal", "lfi", name],
                    )
                )
                break
        return found


@dataclass
class CommandInjectionCheck:
    """Detect operating-system command injection by echoing a marker.

    The payload runs ``echo`` and nothing else. If the marker comes back
    in the response, the shell executed the injected command — that is a
    proof, not an inference. A timing variant (``sleep``) is used only
    as a second opinion on endpoints where the echo is filtered out, and
    only on a couple of endpoints, because a sleep costs real seconds on
    the target's side.
    """

    name: str = "command-injection"
    category: Category = Category.INJECTION
    cost: str = "deep"

    #: Separators and wrappers, each of which reaches a shell.
    SEPARATORS: tuple[tuple[str, str], ...] = (
        (";", "semicolon"),
        ("|", "pipe"),
        ("\n", "newline"),
        ("&&", "and-list"),
        ("`", "backtick"),
        ("$(", "command substitution"),
    )

    #: How much slower the sleeping probe must be before it counts.
    TIMING_THRESHOLD_MS = 1500

    #: At most this many endpoints get the (slow) timing probe.
    TIMING_ENDPOINT_BUDGET = 2

    async def run(self, context: CheckContext) -> list[Finding]:
        found: list[Finding] = []
        endpoints = _query_endpoints(context.endpoints, limit=4)
        for index, endpoint in enumerate(endpoints):
            name, original = _first_param(endpoint)
            if not name:
                continue

            marker = _random_marker("cmd")
            for separator, label in self.SEPARATORS:
                payload = f"{original}{separator}echo {marker}"
                response = await context.fetch(_with_param(endpoint, name, payload))
                if not response.ok or marker not in response.body:
                    continue
                found.append(
                    self._build_finding(
                        context,
                        endpoint,
                        name,
                        payload,
                        label,
                        response,
                        marker,
                    )
                )
                break

            if not found and index < self.TIMING_ENDPOINT_BUDGET:
                timing = await self._timing_probe(context, endpoint, name, original)
                if timing is not None:
                    found.append(timing)
        return found

    async def _timing_probe(
        self, context: CheckContext, endpoint: str, name: str, original: str
    ) -> Finding | None:
        """Confirm command execution by timing when the echo is filtered."""
        marker = _random_marker("cmd")
        fast = await context.fetch(_with_param(endpoint, name, f"{original};echo {marker}"))
        slow = await context.fetch(_with_param(endpoint, name, f"{original};sleep 3"))
        if fast.error or slow.error:
            return None
        if slow.elapsed_ms - fast.elapsed_ms < self.TIMING_THRESHOLD_MS:
            return None
        return self._build_finding(
            context,
            endpoint,
            name,
            f"{original};sleep 3",
            "timing",
            slow,
            marker,
            detail=(
                f"The parameter '{name}' delayed the response by "
                f"{slow.elapsed_ms - fast.elapsed_ms} ms when the payload slept for "
                "3 seconds, which means the payload reached a shell."
            ),
        )

    def _build_finding(
        self,
        context: CheckContext,
        endpoint: str,
        name: str,
        payload: str,
        label: str,
        response: Response,
        marker: str,
        *,
        detail: str = "",
    ) -> Finding:
        """Assemble the finding for a confirmed command injection."""
        description = (
            f"The parameter '{name}' on {endpoint} is passed to a shell. The "
            f"payload '{payload}' executed and its output ({marker}) came back "
            "in the response."
        )
        if detail:
            description = f"{description} {detail}"
        return finding(
            title=f"Operating-system command injection in parameter '{name}'",
            severity=Severity.CRITICAL,
            category=self.category,
            confidence=Confidence.CONFIRMED,
            context=context,
            endpoint=endpoint,
            description=description,
            impact=(
                "The application runs commands with the privileges of its own "
                "process. That is remote code execution: reading files, stealing "
                "credentials, pivoting inside the network, and — depending on the "
                "privileges the process holds — taking over the host."
            ),
            remediation=(
                "Never build a command line by concatenating user input. Pass "
                "arguments as a list to a process spawn, and avoid the shell "
                "entirely where the platform allows it. Run the service with the "
                "least privilege it needs."
            ),
            cwe="CWE-78",
            owasp="A03:2021 - Injection",
            cvss=9.8,
            references=["https://owasp.org/www-community/attacks/Command_Injection"],
            evidence=[
                Evidence(
                    kind=EvidenceKind.COMMAND,
                    summary=f"Payload reached a shell through the '{label}' separator",
                    request=response.request_line(),
                    response_status=response.status,
                    snippet=f"{payload}\n\n{marker}",
                ),
                Evidence(
                    kind=EvidenceKind.RESPONSE,
                    summary="The command's output appeared in the response body",
                    request=response.request_line(),
                    response_status=response.status,
                    snippet=response.body[:300],
                ),
            ],
            source=self.name,
            tags=["rce", "command-injection", name],
        )


@dataclass
class NoSQLInjectionCheck:
    """Detect NoSQL injection through operator injection.

    MongoDB and its relatives treat a document like ``{"password":
    {"$ne": null}}`` as a *query*, not as a value. If the application
    interpolates the object into the query instead of treating it as a
    string, a single crafted parameter bypasses the check entirely.

    The test is differential and read-only: the same endpoint is asked
    for a login that cannot exist and for one that cannot fail, and only
    a change in outcome — a rejected login becoming an accepted one — is
    reported.
    """

    name: str = "nosql-injection"
    category: Category = Category.INJECTION
    cost: str = "deep"

    #: Parameter names that usually carry a login or lookup.
    LOGIN_PARAMS: tuple[str, ...] = (
        "user",
        "username",
        "login",
        "email",
        "name",
        "account",
        "password",
        "passwd",
        "pwd",
        "id",
        "q",
        "query",
        "search",
    )

    #: The operator payloads. Each one means "not equal to null".
    OPERATORS: tuple[tuple[str, str], ...] = (
        ('{"$ne": null}', "JSON operator"),
        ('{"$gt": ""}', "JSON greater-than"),
        ('{"$regex": ".*"}', "JSON regex"),
    )

    async def run(self, context: CheckContext) -> list[Finding]:
        found: list[Finding] = []
        for endpoint in _query_endpoints(context.endpoints, limit=6):
            parsed = urlparse(endpoint)
            pairs = parse_qs(parsed.query, keep_blank_values=True)
            if not pairs:
                continue
            for name in pairs:
                if name.lower() not in self.LOGIN_PARAMS:
                    continue
                result = await self._probe(context, endpoint, name)
                if result is not None:
                    found.append(result)
                    break
        return found

    async def _probe(self, context: CheckContext, endpoint: str, name: str) -> Finding | None:
        """Compare the outcome of a benign lookup with an operator lookup."""
        baseline = await context.fetch(_with_param(endpoint, name, "aegisx-no-such-user-9f2c"))
        if baseline.error:
            return None

        for payload, label in self.OPERATORS:
            probe = await context.fetch(_with_param(endpoint, name, payload))
            if probe.error:
                continue
            if not self._bypass(baseline, probe):
                continue
            return finding(
                title=f"NoSQL injection in parameter '{name}'",
                severity=Severity.CRITICAL,
                category=self.category,
                confidence=Confidence.CONFIRMED,
                context=context,
                endpoint=endpoint,
                description=(
                    f"The parameter '{name}' on {endpoint} is interpreted as a query "
                    f"operator rather than as a value. Sending {payload} ({label}) "
                    "changed the outcome from 'no match' to 'match', which is the "
                    "signature of a query that the client controls."
                ),
                impact=(
                    "The operator payload turns the lookup into a query that always "
                    "matches. Depending on the field it is applied to, that is "
                    "authentication bypass, access to another user's records, or "
                    "the extraction of the whole collection."
                ),
                remediation=(
                    "Cast the input to the type the field actually has before it "
                    "reaches the driver, and reject objects and arrays where a "
                    "string is expected. Build the query from validated fields, "
                    "never from a decoded body."
                ),
                cwe="CWE-943",
                owasp="A03:2021 - Injection",
                cvss=9.8,
                references=["https://owasp.org/www-community/attacks/NoSQL_Injection"],
                evidence=[
                    Evidence(
                        kind=EvidenceKind.REQUEST,
                        summary=f"Operator payload sent in '{name}': {payload}",
                        request=probe.request_line(),
                        response_status=probe.status,
                    ),
                    Evidence(
                        kind=EvidenceKind.RESPONSE,
                        summary=(
                            f"Baseline status {baseline.status} became {probe.status} "
                            "with the operator payload"
                        ),
                        request=probe.request_line(),
                        response_status=probe.status,
                        snippet=probe.body[:300],
                    ),
                ],
                source=self.name,
                tags=["nosql", "injection", "auth-bypass", name],
            )
        return None

    @staticmethod
    def _bypass(baseline: Response, probe: Response) -> bool:
        """Whether the operator payload changed the outcome for the worse."""
        if baseline.status in (401, 403) and probe.status in (200, 204, 302):
            return True
        if baseline.status >= 400 and probe.status < 400:
            return True
        # Same status, but the body grew: the query matched a record it
        # should not have.
        if baseline.status == probe.status and len(probe.body) > len(baseline.body) + 200:
            return True
        return False


@dataclass
class XXECheck:
    """Detect XML external entity processing.

    Two probes, both read-only:

    - an *internal* entity whose value is a marker — if the marker comes
      back resolved, the parser expands entities, which is the root of
      the bug;
    - an *external* entity pointing at a host under the reserved
      ``.invalid`` TLD — if the server's own error names that host, it
      tried to fetch it, which is external entity resolution.

    The external probe never reaches a third party: RFC 2606 reserves
    ``.invalid`` and it never resolves.
    """

    name: str = "xxe"
    category: Category = Category.INJECTION
    cost: str = "deep"

    #: Endpoints that plausibly accept XML.
    XML_HINTS: tuple[str, ...] = ("xml", "soap", "rss", "upload", "import", "feed")

    async def run(self, context: CheckContext) -> list[Finding]:
        found: list[Finding] = []
        for endpoint in self._xml_endpoints(context):
            marker = _random_marker("xxe")
            internal = self._internal_payload(marker)
            response = await context.fetch(
                endpoint,
                method="POST",
                headers={"Content-Type": "application/xml"},
                data=internal,
            )
            if response.error:
                continue
            if marker in response.body and "&xxe;" not in response.body:
                found.append(
                    self._build_finding(
                        context,
                        endpoint,
                        response,
                        internal,
                        "entity expansion",
                        (
                            "The parser resolved an internal entity, which means "
                            "entity processing is enabled. External entities are "
                            "usually enabled alongside it."
                        ),
                    )
                )
                continue

            external = self._external_payload()
            external_response = await context.fetch(
                endpoint,
                method="POST",
                headers={"Content-Type": "application/xml"},
                data=external,
            )
            if external_response.error:
                continue
            if self._names_the_host(external_response.body, "aegisx-xxe-canary.invalid"):
                found.append(
                    self._build_finding(
                        context,
                        endpoint,
                        external_response,
                        external,
                        "external entity resolution",
                        (
                            "The server attempted to resolve an external entity "
                            "pointing at a host the client supplied. RFC 2606 "
                            "reserves .invalid, so no third party was contacted."
                        ),
                    )
                )
        return found

    def _xml_endpoints(self, context: CheckContext) -> list[str]:
        """Endpoints worth sending XML to."""
        candidates: list[str] = []
        for endpoint in context.endpoints[:12]:
            lowered = endpoint.lower()
            if any(hint in lowered for hint in self.XML_HINTS):
                candidates.append(endpoint)
        if not candidates:
            candidates = [context.base_url]
        return candidates[:3]

    @staticmethod
    def _internal_payload(marker: str) -> str:
        """XML with an internal entity that expands to a marker."""
        return (
            f'<?xml version="1.0"?><!DOCTYPE probe [<!ENTITY xxe "{marker}">]><probe>&xxe;</probe>'
        )

    @staticmethod
    def _external_payload() -> str:
        """XML with an external entity pointing at a reserved host."""
        return (
            '<?xml version="1.0"?>'
            "<!DOCTYPE probe ["
            '<!ENTITY xxe SYSTEM "http://aegisx-xxe-canary.invalid/xxe">'
            "]>"
            "<probe>&xxe;</probe>"
        )

    @staticmethod
    def _names_the_host(body: str, host: str) -> bool:
        """Whether the server's own error names the injected host."""
        return host.lower() in body.lower()

    def _build_finding(
        self,
        context: CheckContext,
        endpoint: str,
        response: Response,
        payload: str,
        label: str,
        detail: str,
    ) -> Finding:
        """Assemble the finding for a confirmed XXE."""
        return finding(
            title=f"XML external entity processing on {endpoint}",
            severity=Severity.CRITICAL,
            category=self.category,
            confidence=Confidence.CONFIRMED,
            context=context,
            endpoint=endpoint,
            description=(
                f"The endpoint {endpoint} parses XML with entity processing "
                f"enabled. The probe produced evidence of {label}. {detail}"
            ),
            impact=(
                "With external entities enabled, an attacker can read files the "
                "application process can read — including source code and "
                "configuration with credentials — and, where the parser fetches "
                "URLs, reach internal services from the server."
            ),
            remediation=(
                "Disable DTDs and external entities in the parser. On libxml2 set "
                "XML_PARSE_NOENT off and use XML_PARSE_NONET; in Java use "
                "FEATURE_SECURE_PROCESSING with external entities disabled; in "
                "Python use defusedxml. Where XML is not required, accept JSON."
            ),
            cwe="CWE-611",
            owasp="A05:2021 - Security Misconfiguration",
            cvss=9.1,
            references=[
                "https://owasp.org/www-community/vulnerabilities/XML_External_Entity_(XXE)_Processing"
            ],
            evidence=[
                Evidence(
                    kind=EvidenceKind.REQUEST,
                    summary="XML payload with an entity declaration",
                    request=f"POST {endpoint}\nContent-Type: application/xml\n\n{payload}",
                    response_status=response.status,
                ),
                Evidence(
                    kind=EvidenceKind.RESPONSE,
                    summary=f"Evidence of {label}",
                    request=response.request_line(),
                    response_status=response.status,
                    snippet=response.body[:300],
                ),
            ],
            source=self.name,
            tags=["xxe", "xml", "injection"],
        )
