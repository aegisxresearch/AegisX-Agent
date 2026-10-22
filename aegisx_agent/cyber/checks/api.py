"""API surface checks — exposed administration panels and GraphQL.

Modern applications leak their internals through predictable paths.
An administration panel left reachable, a debug endpoint that returns a
stack trace, or a GraphQL schema that can be read in full, each give an
attacker a map of the application before they send a single exploit.

Every probe here is a read: a ``GET`` for a path, or a ``POST`` that
carries an introspection query and changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aegisx_agent.cyber.checks import CheckContext, finding
from aegisx_agent.cyber.models import (
    Category,
    Confidence,
    Evidence,
    EvidenceKind,
    Finding,
    Severity,
)

#: Paths that expose administration or debugging, with what each one
#: usually leaks. Every path is a GET.
ADMIN_PATHS: tuple[tuple[str, str, str], ...] = (
    ("/admin", "administration panel", "administrative functionality is reachable"),
    ("/administrator", "administration panel", "administrative functionality is reachable"),
    ("/admin/login", "administration login", "an administrative login is exposed"),
    ("/debug", "debug endpoint", "internal debug output is exposed"),
    ("/actuator", "management actuator", "application internals are exposed"),
    ("/actuator/env", "management actuator", "environment variables may be exposed"),
    ("/actuator/health", "management actuator", "the service's health and internals are exposed"),
    ("/server-status", "server status page", "server internals are exposed"),
    ("/server-info", "server info page", "server internals are exposed"),
    ("/phpinfo.php", "PHP configuration page", "the PHP configuration is exposed"),
    ("/metrics", "metrics endpoint", "operational metrics are exposed"),
    ("/console", "interactive console", "an interactive console is exposed"),
    ("/swagger-ui/", "API documentation UI", "the API surface is documented"),
    ("/api-docs", "API documentation UI", "the API surface is documented"),
    ("/graphql", "GraphQL endpoint", "the GraphQL API is reachable"),
    ("/graphiql", "GraphQL IDE", "the GraphQL IDE is reachable"),
)

#: The introspection query. It reads the schema and changes nothing.
INTROSPECTION_QUERY = (
    '{"query":"query IntrospectionQuery { __schema { queryType { name } '
    'mutationType { name } types { name kind } } }"}'
)

#: Markers that show the introspection query was actually answered.
INTROSPECTION_MARKERS: tuple[str, ...] = (
    '"__schema"',
    '"queryType"',
    '"mutationType"',
    '"types"',
)


@dataclass
class AdminSurfaceCheck:
    """Look for administration, debug, and documentation surfaces left open.

    A path counts as exposed only when the response is a real page
    (status 200) and the body contains the marker that identifies it —
    not merely when the server answers with a soft 404 that happens to
    return 200 for everything.
    """

    name: str = "admin-surface"
    category: Category = Category.EXPOSURE
    cost: str = "cheap"

    async def run(self, context: CheckContext) -> list[Finding]:
        found: list[Finding] = []
        base = context.base_url.rstrip("/")
        for path, label, consequence in ADMIN_PATHS:
            url = f"{base}{path}"
            response = await context.fetch(url)
            if response.error or response.status != 200:
                continue
            if not self._looks_real(response, path):
                continue

            severity = Severity.MEDIUM
            if path.startswith("/actuator/env") or path.startswith("/console"):
                severity = Severity.HIGH

            found.append(
                finding(
                    title=f"Exposed {label} at {path}",
                    severity=severity,
                    category=self.category,
                    confidence=Confidence.CONFIRMED,
                    context=context,
                    endpoint=url,
                    description=(
                        f"The path {path} is served with status 200 and returns "
                        f"content that identifies it as a {label}. {consequence.capitalize()}."
                    ),
                    impact=(
                        "An exposed administration or debug surface hands an "
                        "attacker the application's own view of itself: the "
                        "configuration, the routes, the internal state, and "
                        "sometimes credentials."
                    ),
                    remediation=(
                        "Restrict the surface to a private network or an "
                        "authenticated role, and deny it at the edge. Make the "
                        "path unguessable only as a second layer — obscurity "
                        "alone is not a control."
                    ),
                    cwe="CWE-200",
                    owasp="A01:2021 - Broken Access Control",
                    cvss=5.3,
                    references=["https://cwe.mitre.org/data/definitions/200.html"],
                    evidence=[
                        Evidence(
                            kind=EvidenceKind.RESPONSE,
                            summary=f"{label} returned status 200",
                            request=response.request_line(),
                            response_status=response.status,
                            snippet=response.body[:300],
                        )
                    ],
                    source=self.name,
                    tags=["exposure", "admin", path.strip("/")],
                )
            )
        return found

    @staticmethod
    def _looks_real(response: Any, path: str) -> bool:
        """Whether the body looks like the surface it claims to be."""
        body = response.body
        lowered = body.lower()
        if not body.strip():
            return False
        # A catch-all that answers 200 for everything is not an exposure.
        if "not found" in lowered and len(body) < 400:
            return False
        hints = {
            "/actuator": ("actuator", "management"),
            "/metrics": ("prometheus", "metric"),
            "/phpinfo.php": ("php version", "configuration"),
            "/server-status": ("apache server status", "server status"),
            "/server-info": ("server info", "server information"),
            "/graphql": ("graphql", "query"),
            "/graphiql": ("graphiql", "graphql"),
            "/swagger-ui": ("swagger", "openapi"),
            "/api-docs": ("swagger", "openapi", "redoc"),
            "/admin": ("admin", "login", "sign in", "dashboard", "password"),
            "/administrator": ("admin", "login", "sign in"),
            "/debug": ("debug", "stack trace", "traceback"),
            "/console": ("console", "terminal", "shell"),
        }
        for prefix, markers in hints.items():
            if path.startswith(prefix) and any(marker in lowered for marker in markers):
                return True
        return False


@dataclass
class GraphQLCheck:
    """Test whether a GraphQL endpoint exposes its schema.

    Introspection is not a vulnerability on its own — it is how GraphQL
    clients discover the schema. It becomes one when the endpoint is
    reachable without authentication, because the schema then names
    every query, every mutation, and every field the attacker may try.
    """

    name: str = "graphql-introspection"
    category: Category = Category.EXPOSURE
    cost: str = "cheap"

    async def run(self, context: CheckContext) -> list[Finding]:
        found: list[Finding] = []
        for path in ("/graphql", "/api/graphql", "/v1/graphql", "/query"):
            url = f"{context.base_url.rstrip('/')}{path}"
            response = await context.fetch(
                url,
                method="POST",
                headers={"Content-Type": "application/json"},
                data=INTROSPECTION_QUERY,
            )
            if response.error or response.status >= 400:
                continue
            if not any(marker in response.body for marker in INTROSPECTION_MARKERS):
                continue

            found.append(
                finding(
                    title=f"GraphQL schema exposed through introspection at {path}",
                    severity=Severity.LOW,
                    category=self.category,
                    confidence=Confidence.CONFIRMED,
                    context=context,
                    endpoint=url,
                    description=(
                        f"The GraphQL endpoint at {path} answered an introspection "
                        "query without authentication and returned the schema."
                    ),
                    impact=(
                        "The schema is the attack surface in full: every query, "
                        "mutation, argument, and type the application supports. "
                        "It turns guessing into reading."
                    ),
                    remediation=(
                        "Disable introspection in production, or gate it behind an "
                        "authenticated role. Where clients legitimately need it, "
                        "enable it only for staff accounts."
                    ),
                    cwe="CWE-200",
                    owasp="A01:2021 - Broken Access Control",
                    cvss=3.7,
                    references=["https://owasp.org/www-project-web-security-testing-guide/"],
                    evidence=[
                        Evidence(
                            kind=EvidenceKind.REQUEST,
                            summary="Introspection query posted to the endpoint",
                            request=(
                                f"POST {url}\nContent-Type: application/json\n\n"
                                f"{INTROSPECTION_QUERY}"
                            ),
                            response_status=response.status,
                        ),
                        Evidence(
                            kind=EvidenceKind.RESPONSE,
                            summary="Schema returned",
                            request=response.request_line(),
                            response_status=response.status,
                            snippet=response.body[:300],
                        ),
                    ],
                    source=self.name,
                    tags=["graphql", "introspection", "schema"],
                )
            )
            break
        return found
