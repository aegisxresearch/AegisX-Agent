"""Exposure checks — what the target accidentally publishes.

A surprising share of real breaches start here, not with a clever
injection: a source map, a ``.git`` directory, a backup file, a
directory listing, or a stack trace that names the framework and the
file layout. These checks are cheap, produce few false positives, and
give an attacker the map that makes the harder checks work.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from aegisx_agent.cyber.checks import CheckContext, finding
from aegisx_agent.cyber.models import (
    Category,
    Confidence,
    Evidence,
    EvidenceKind,
    Finding,
    Severity,
)

#: Paths that should never be publicly reachable, with what leaks if they are.
SENSITIVE_PATHS: tuple[tuple[str, str, Severity, str], ...] = (
    (
        "/.git/config",
        "Git configuration exposing the repository remote and branch layout",
        Severity.HIGH,
        "CWE-527",
    ),
    ("/.git/HEAD", "Git HEAD exposing the current branch and commit", Severity.MEDIUM, "CWE-527"),
    (
        "/.env",
        "Environment file, typically holding API keys and database credentials",
        Severity.CRITICAL,
        "CWE-200",
    ),
    (
        "/.env.local",
        "Local environment file with development credentials",
        Severity.HIGH,
        "CWE-200",
    ),
    ("/.env.production", "Production environment file", Severity.CRITICAL, "CWE-200"),
    ("/config.json", "Application configuration that may hold secrets", Severity.MEDIUM, "CWE-200"),
    ("/config.php", "Application configuration in PHP", Severity.HIGH, "CWE-200"),
    (
        "/wp-config.php",
        "WordPress configuration with database credentials and salts",
        Severity.CRITICAL,
        "CWE-200",
    ),
    ("/.aws/credentials", "AWS credentials file", Severity.CRITICAL, "CWE-200"),
    ("/.ssh/id_rsa", "SSH private key", Severity.CRITICAL, "CWE-200"),
    (
        "/backup.zip",
        "Backup archive that may contain the full application and database",
        Severity.HIGH,
        "CWE-527",
    ),
    ("/backup.sql", "Database dump", Severity.CRITICAL, "CWE-200"),
    ("/dump.sql", "Database dump", Severity.CRITICAL, "CWE-200"),
    ("/database.sql", "Database dump", Severity.CRITICAL, "CWE-200"),
    (
        "/phpinfo.php",
        "PHP configuration page exposing every environment variable",
        Severity.HIGH,
        "CWE-200",
    ),
    (
        "/server-status",
        "Apache status page exposing internal request details",
        Severity.MEDIUM,
        "CWE-200",
    ),
    ("/server-info", "Apache server information page", Severity.MEDIUM, "CWE-200"),
    (
        "/actuator/env",
        "Spring Boot actuator exposing environment variables and secrets",
        Severity.CRITICAL,
        "CWE-200",
    ),
    (
        "/actuator/heapdump",
        "Spring Boot heap dump containing in-memory secrets",
        Severity.CRITICAL,
        "CWE-200",
    ),
    (
        "/api/v1/swagger.json",
        "API specification revealing the full attack surface",
        Severity.LOW,
        "CWE-200",
    ),
    (
        "/swagger.json",
        "API specification revealing the full attack surface",
        Severity.LOW,
        "CWE-200",
    ),
    (
        "/openapi.json",
        "API specification revealing the full attack surface",
        Severity.LOW,
        "CWE-200",
    ),
    (
        "/.well-known/security.txt",
        "Security contact file (informational, not a finding)",
        Severity.INFO,
        "CWE-200",
    ),
    (
        "/Dockerfile",
        "Dockerfile revealing the build and runtime layout",
        Severity.MEDIUM,
        "CWE-200",
    ),
    (
        "/docker-compose.yml",
        "Compose file revealing service topology and credentials",
        Severity.HIGH,
        "CWE-200",
    ),
    (
        "/package.json",
        "Dependency manifest revealing the exact library versions",
        Severity.LOW,
        "CWE-200",
    ),
    ("/composer.lock", "PHP dependency lockfile with exact versions", Severity.LOW, "CWE-200"),
    ("/id_rsa", "SSH private key at the web root", Severity.CRITICAL, "CWE-200"),
    ("/.DS_Store", "macOS directory metadata listing filenames", Severity.LOW, "CWE-200"),
    ("/WEB-INF/web.xml", "Java web application descriptor", Severity.MEDIUM, "CWE-200"),
    (
        "/crossdomain.xml",
        "Flash cross-domain policy allowing unrestricted access",
        Severity.MEDIUM,
        "CWE-942",
    ),
)

#: Signatures that mark a body as a directory listing.
DIRECTORY_LISTING_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("<title>index of /", "Apache-style index"),
    ("<h1>index of /", "nginx-style index"),
    ("directory listing for", "Python SimpleHTTPServer"),
    ("<title>Directory listing for /", "Apache autoindex"),
    ("[parent directory]", "bare-bones listing"),
)

#: Framework error markers that leak internals.
ERROR_SIGNATURES: tuple[tuple[str, str, str], ...] = (
    ("traceback (most recent call last)", "Python stack trace", "PHP"),
    ("java.lang.nullpointerexception", "Java exception", "Java"),
    ("org.springframework", "Spring stack trace", "Java"),
    ("javax.servlet", "Servlet container trace", "Java"),
    ("<b>fatal error</b>", "PHP fatal error", "PHP"),
    ("on line <b>", "PHP error with source line", "PHP"),
    ("syntax error, unexpected", "PHP parse error", "PHP"),
    ("microsoft ole db provider for sql server", "SQL Server error", "ASP.NET"),
    ("asp.net application error", "ASP.NET error page", "ASP.NET"),
    ("rails.application::routingerror", "Ruby on Rails routing error", "Ruby"),
    ("actioncontroller::routingerror", "Ruby on Rails routing error", "Ruby"),
    ("werkzeug debugger", "Werkzeug interactive debugger", "Python"),
    ("gorilla/mux", "Go routing panic", "Go"),
    ("panic: runtime error", "Go panic", "Go"),
)


@dataclass
class SensitiveFileCheck:
    """Probe for well-known files that should not be web-reachable."""

    name: str = "sensitive-files"
    category: Category = Category.EXPOSURE
    cost: str = "cheap"

    async def run(self, context: CheckContext) -> list[Finding]:
        found: list[Finding] = []
        for path, description, severity, cwe in SENSITIVE_PATHS:
            url = urljoin(context.base_url.rstrip("/") + "/", path.lstrip("/"))
            response = await context.fetch(url, method="GET")
            if not response.ok or response.status != 200:
                continue
            body = response.body
            if not body.strip():
                continue
            # A soft-404 page that merely mentions the path is not a leak.
            if response.status == 200 and len(body) < 64 and "not found" in body.lower():
                continue

            found.append(
                finding(
                    title=f"Exposed file: {path}",
                    severity=severity,
                    category=self.category,
                    confidence=Confidence.CONFIRMED,
                    context=context,
                    endpoint=url,
                    description=f"{path} is publicly reachable. {description}.",
                    impact=(
                        "Anything served from this path is available to anyone who "
                        "knows or guesses the URL, and it often contains credentials "
                        "or the source needed to plan further attacks."
                    ),
                    remediation=(
                        f"Remove {path} from the web root, deny it at the web server "
                        "or CDN, and rotate any credential that may have been "
                        "exposed by it."
                    ),
                    cwe=cwe,
                    owasp="A01:2021 - Broken Access Control",
                    cvss=7.5 if severity is Severity.HIGH else 5.3,
                    evidence=[
                        Evidence(
                            kind=EvidenceKind.RESPONSE,
                            summary=f"{path} returned HTTP {response.status}",
                            request=response.request_line(),
                            response_status=response.status,
                            snippet=body[:500],
                        )
                    ],
                    source=self.name,
                    tags=["exposure", path.strip("/").split("/")[0] or "root"],
                )
            )
        return found


@dataclass
class DirectoryListingCheck:
    """Detect exposed directory listings."""

    name: str = "directory-listing"
    category: Category = Category.EXPOSURE
    cost: str = "cheap"

    #: Common directories that should not be browsable.
    CANDIDATES: tuple[str, ...] = ("/", "/uploads/", "/backup/", "/files/", "/static/", "/assets/")

    async def run(self, context: CheckContext) -> list[Finding]:
        found: list[Finding] = []
        for path in self.CANDIDATES:
            url = urljoin(context.base_url.rstrip("/") + "/", path.lstrip("/"))
            response = await context.fetch(url, method="GET")
            if not response.ok:
                continue
            lowered = response.body.lower()
            signature = ""
            for pattern, label in DIRECTORY_LISTING_SIGNATURES:
                if pattern in lowered:
                    signature = label
                    break
            if not signature:
                continue
            found.append(
                finding(
                    title=f"Directory listing enabled at {path}",
                    severity=Severity.MEDIUM,
                    category=self.category,
                    confidence=Confidence.CONFIRMED,
                    context=context,
                    endpoint=url,
                    description=(
                        f"{path} returns a browsable index ({signature}) instead of a "
                        "denial or a landing page."
                    ),
                    impact=(
                        "The listing enumerates files that were meant to be "
                        "unlinked — uploads, backups, and drafts — turning a blind "
                        "guess into a directory walk."
                    ),
                    remediation=(
                        "Disable directory indexing in the web server "
                        "(autoindex off) and serve directories through an "
                        "application route that authorises access."
                    ),
                    cwe="CWE-548",
                    owasp="A01:2021 - Broken Access Control",
                    cvss=5.3,
                    evidence=[
                        Evidence(
                            kind=EvidenceKind.RESPONSE,
                            summary=f"{signature} detected at {path}",
                            request=response.request_line(),
                            response_status=response.status,
                            snippet=response.body[:400],
                        )
                    ],
                    source=self.name,
                    tags=["exposure", "directory-listing"],
                )
            )
        return found


@dataclass
class VerboseErrorCheck:
    """Detect stack traces and framework error pages that leak internals."""

    name: str = "verbose-errors"
    category: Category = Category.EXPOSURE
    cost: str = "cheap"

    async def run(self, context: CheckContext) -> list[Finding]:
        found: list[Finding] = []
        endpoints = [context.base_url, *context.endpoints[:5]]
        seen: set[str] = set()
        for url in endpoints:
            if url in seen:
                continue
            seen.add(url)
            response = await context.fetch(url, method="GET")
            if not response.ok and response.status < 400:
                continue
            lowered = response.body.lower()
            for pattern, label, language in ERROR_SIGNATURES:
                if pattern not in lowered:
                    continue
                found.append(
                    finding(
                        title=f"Verbose error disclosure on {urlparse(url).path or '/'}",
                        severity=Severity.MEDIUM,
                        category=self.category,
                        confidence=Confidence.CONFIRMED,
                        context=context,
                        endpoint=url,
                        description=(
                            f"The response body contains a {label} ({language}), which "
                            "reveals internal file paths, framework versions, and "
                            "sometimes configuration values."
                        ),
                        impact=(
                            "Stack traces hand an attacker the source layout, the "
                            "framework version to target, and the exact exception "
                            "type — all of which narrow the next attack from "
                            "theoretical to specific."
                        ),
                        remediation=(
                            "Serve a generic error page to clients and log the detail "
                            "server-side. Set the framework to production mode, which "
                            "suppresses debug pages and tracebacks."
                        ),
                        cwe="CWE-209",
                        owasp="A05:2021 - Security Misconfiguration",
                        cvss=5.3,
                        evidence=[
                            Evidence(
                                kind=EvidenceKind.RESPONSE,
                                summary=f"{label} detected (HTTP {response.status})",
                                request=response.request_line(),
                                response_status=response.status,
                                snippet=response.body[:600],
                            )
                        ],
                        source=self.name,
                        tags=["exposure", "information-disclosure", language.lower()],
                    )
                )
                break
        return found


@dataclass
class CORSCheck:
    """Detect permissive CORS configuration.

    The dangerous case is a wildcard origin *combined* with credentials:
    a browser refuses that combination outright, but the two halves
    shipped separately — ``Access-Control-Allow-Origin: <reflected
    origin>`` plus ``Allow-Credentials: true`` — is the configuration
    that actually lets an attacker's page read authenticated responses.
    """

    name: str = "cors-misconfiguration"
    category: Category = Category.CORS
    cost: str = "cheap"

    async def run(self, context: CheckContext) -> list[Finding]:
        found: list[Finding] = []
        evil = "https://aegisx-probe.invalid"
        response = await context.fetch(context.base_url, headers={"Origin": evil})
        if not response.ok:
            return []

        allow_origin = response.header("access-control-allow-origin", "")
        allow_credentials = response.header("access-control-allow-credentials", "").lower()
        if not allow_origin:
            return []

        reflected = allow_origin.strip() == evil
        wildcard = allow_origin.strip() == "*"
        credentials = allow_credentials == "true"

        if wildcard and not credentials:
            return [
                finding(
                    title="CORS allows any origin (wildcard)",
                    severity=Severity.LOW,
                    category=self.category,
                    confidence=Confidence.CONFIRMED,
                    context=context,
                    description=(
                        "The API answers with 'Access-Control-Allow-Origin: *', so any "
                        "website can read responses from it."
                    ),
                    impact=(
                        "If any response carries data tied to the user's session, a "
                        "cross-origin page can read it. Without credentials this is "
                        "limited to public data, so the severity is low — but it "
                        "becomes critical the moment credentials are allowed."
                    ),
                    remediation=(
                        "List the specific origins that need access, or reflect the "
                        "origin only after checking it against an allow-list."
                    ),
                    cwe="CWE-942",
                    owasp="A05:2021 - Security Misconfiguration",
                    evidence=[
                        Evidence(
                            kind=EvidenceKind.RESPONSE,
                            summary="Access-Control-Allow-Origin: *",
                            request=response.request_line(),
                            response_status=response.status,
                        )
                    ],
                    source=self.name,
                    tags=["cors"],
                )
            ]

        if reflected and credentials:
            found.append(
                finding(
                    title="CORS reflects arbitrary origins with credentials enabled",
                    severity=Severity.HIGH,
                    category=self.category,
                    confidence=Confidence.CONFIRMED,
                    context=context,
                    description=(
                        "The API reflects any requested Origin and also sets "
                        "'Access-Control-Allow-Credentials: true', so a page on any "
                        "domain can make authenticated requests and read the answers."
                    ),
                    impact=(
                        "This is the classic cross-origin data theft primitive: an "
                        "attacker hosts a page, the victim's browser calls this API "
                        "with the victim's cookies, and the attacker reads the "
                        "response. Session-protected data, including account and "
                        "billing details, is exposed to any site the victim visits."
                    ),
                    remediation=(
                        "Validate the Origin header against a strict allow-list "
                        "before reflecting it, and never combine credentialed CORS "
                        "with a wildcard or reflected origin."
                    ),
                    cwe="CWE-346",
                    owasp="A05:2021 - Security Misconfiguration",
                    cvss=8.1,
                    references=["https://developer.mozilla.org/docs/Web/HTTP/CORS"],
                    evidence=[
                        Evidence(
                            kind=EvidenceKind.RESPONSE,
                            summary=(
                                f"Access-Control-Allow-Origin: {allow_origin} / "
                                f"Access-Control-Allow-Credentials: {allow_credentials}"
                            ),
                            request=f"{response.request_line()}\nOrigin: {evil}",
                            response_status=response.status,
                        )
                    ],
                    source=self.name,
                    tags=["cors", "credential-theft"],
                )
            )
        elif reflected:
            found.append(
                finding(
                    title="CORS reflects arbitrary origins",
                    severity=Severity.MEDIUM,
                    category=self.category,
                    confidence=Confidence.CONFIRMED,
                    context=context,
                    description=(
                        "The API reflects any requested Origin header without "
                        "validating it against an allow-list."
                    ),
                    impact=(
                        "Any website can read responses from this origin. If "
                        "authentication is added later, or if any response depends on "
                        "the victim's session, this becomes a data theft primitive."
                    ),
                    remediation=(
                        "Validate the Origin header against a strict allow-list of "
                        "the frontends that legitimately call this API."
                    ),
                    cwe="CWE-346",
                    owasp="A05:2021 - Security Misconfiguration",
                    cvss=6.5,
                    evidence=[
                        Evidence(
                            kind=EvidenceKind.RESPONSE,
                            summary=f"Access-Control-Allow-Origin: {allow_origin}",
                            request=f"{response.request_line()}\nOrigin: {evil}",
                            response_status=response.status,
                        )
                    ],
                    source=self.name,
                    tags=["cors"],
                )
            )
        return found
