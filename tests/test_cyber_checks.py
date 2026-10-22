"""Each cyber check, exercised against a scripted target.

The checks are the part of the engine that decides what is true, so the
tests here assert on the *reason* a finding exists rather than only on
its count: a header check fires because the header is absent, an
injection check fires because the differential proved it, and a soft-404
page is never reported as an exposure.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from aegisx_agent.cyber.checks import CheckContext, run_check, run_checks
from aegisx_agent.cyber.checks.api import AdminSurfaceCheck, GraphQLCheck
from aegisx_agent.cyber.checks.authn import TokenCheck
from aegisx_agent.cyber.checks.authz import (
    HTTPMethodCheck,
    IDORCheck,
    OpenRedirectCheck,
    TransportCheck,
)
from aegisx_agent.cyber.checks.dns import CloudStorageCheck, SubdomainTakeoverCheck
from aegisx_agent.cyber.checks.exposure import (
    CORSCheck,
    DirectoryListingCheck,
    SensitiveFileCheck,
    VerboseErrorCheck,
)
from aegisx_agent.cyber.checks.headers import CookieCheck, HeaderCheck
from aegisx_agent.cyber.checks.injection import (
    CommandInjectionCheck,
    NoSQLInjectionCheck,
    PathTraversalCheck,
    SQLInjectionCheck,
    TemplateInjectionCheck,
    XSSCheck,
    XXECheck,
)
from aegisx_agent.cyber.checks.protocol import CRLFInjectionCheck, HostHeaderCheck
from aegisx_agent.cyber.checks.ssrf import SSRFCheck
from aegisx_agent.cyber.models import Category, Confidence, Severity
from aegisx_agent.cyber.prober import Response

BASE = "https://app.example.com"


def response(
    url: str,
    status: int = 200,
    body: str = "",
    *,
    headers: dict[str, str] | None = None,
    cookies: list[str] | None = None,
    content_type: str = "text/html",
) -> Response:
    """Build a captured response the way the prober would."""
    return Response(
        url=url,
        status=status,
        headers={key.lower(): value for key, value in (headers or {}).items()},
        body=body,
        content_type=content_type,
        multi_headers={"set-cookie": list(cookies or [])} if cookies else {},
    )


class FakeProber:
    """A prober that answers from a script instead of the network."""

    def __init__(self, handler: Callable[..., Response]) -> None:
        self.handler = handler
        self.calls: list[dict[str, Any]] = []

    async def request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        data: str | bytes | None = None,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        follow_redirects: bool | None = None,
        timeout: float | None = None,
    ) -> Response:
        call = {"method": method, "url": url, "headers": dict(headers or {}), "data": data}
        self.calls.append(call)
        return self.handler(**call)


def scripted(handler: Callable[..., Response]) -> tuple[FakeProber, CheckContext]:
    """Build a context whose prober runs ``handler``."""
    prober = FakeProber(handler)
    context = CheckContext(
        base_url=BASE,
        host="app.example.com",
        prober=prober,  # type: ignore[arg-type]
        endpoints=[BASE],
    )
    return prober, context


def query(url: str) -> dict[str, str]:
    """The first value of every query parameter, decoded."""
    return {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}


def titles(findings: list[Any]) -> list[str]:
    return [finding.title for finding in findings]


# --------------------------------------------------------------------------
# Headers and cookies
# --------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_missing_security_headers_are_reported_one_by_one() -> None:
    _, context = scripted(lambda **call: response(call["url"], body="<html>ok</html>"))

    findings = await HeaderCheck().run(context)

    found = titles(findings)
    assert "Missing security header: Content-Security-Policy" in found
    assert "Missing security header: Strict-Transport-Security" in found
    assert "Missing security header: X-Frame-Options" in found
    assert all(finding.source == "security-headers" for finding in findings)


@pytest.mark.asyncio()
async def test_a_fully_configured_origin_produces_no_header_findings() -> None:
    headers = {
        "Content-Security-Policy": "default-src 'self'",
        "Strict-Transport-Security": "max-age=31536000",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "geolocation=()",
    }
    _, context = scripted(lambda **call: response(call["url"], headers=headers, body="ok"))

    assert await HeaderCheck().run(context) == []


@pytest.mark.asyncio()
async def test_hsts_is_not_demanded_over_plain_http() -> None:
    prober = FakeProber(lambda **call: response(call["url"], body="ok"))
    context = CheckContext(
        base_url="http://app.example.com",
        host="app.example.com",
        prober=prober,  # type: ignore[arg-type]
    )

    findings = await HeaderCheck().run(context)

    assert "Missing security header: Strict-Transport-Security" not in titles(findings)


@pytest.mark.asyncio()
async def test_a_version_disclosing_header_is_reported_as_information_leak() -> None:
    _, context = scripted(
        lambda **call: response(call["url"], headers={"Server": "nginx/1.18.0"}, body="ok")
    )

    findings = await HeaderCheck().run(context)

    assert any("Server header" in title for title in titles(findings))


@pytest.mark.asyncio()
async def test_a_cookie_without_flags_is_reported_with_its_worst_missing_flag() -> None:
    _, context = scripted(
        lambda **call: response(call["url"], cookies=["session=abc123"], body="ok")
    )

    findings = await CookieCheck().run(context)

    assert len(findings) == 1
    assert "session" in findings[0].title
    assert findings[0].severity is Severity.MEDIUM
    assert findings[0].category is Category.COOKIE


@pytest.mark.asyncio()
async def test_a_hardened_cookie_is_not_reported() -> None:
    _, context = scripted(
        lambda **call: response(
            call["url"], cookies=["session=abc; Secure; HttpOnly; SameSite=Lax"], body="ok"
        )
    )

    assert await CookieCheck().run(context) == []


# --------------------------------------------------------------------------
# Exposure
# --------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_a_reachable_env_file_is_a_critical_finding() -> None:
    def handler(url: str, **_: Any) -> Response:
        if url.endswith("/.env"):
            return response(url, body="DB_PASSWORD=hunter2")
        return response(url, 404, "not found")

    _, context = scripted(handler)

    findings = await SensitiveFileCheck().run(context)

    assert titles(findings) == ["Exposed file: /.env"]
    assert findings[0].severity is Severity.CRITICAL
    assert findings[0].evidence[0].snippet == "DB_PASSWORD=hunter2"


@pytest.mark.asyncio()
async def test_a_soft_404_is_not_an_exposure() -> None:
    def handler(url: str, **_: Any) -> Response:
        return response(url, 200, "Page not found")

    _, context = scripted(handler)

    assert await SensitiveFileCheck().run(context) == []


@pytest.mark.asyncio()
async def test_an_apache_index_is_a_directory_listing() -> None:
    def handler(url: str, **_: Any) -> Response:
        if url.rstrip("/") == BASE:
            return response(url, body="<title>Index of /</title>")
        return response(url, 404, "")

    _, context = scripted(handler)

    findings = await DirectoryListingCheck().run(context)

    assert titles(findings) == ["Directory listing enabled at /"]
    assert findings[0].category is Category.EXPOSURE


@pytest.mark.asyncio()
async def test_a_stack_trace_is_a_verbose_error_finding() -> None:
    _, context = scripted(
        lambda **call: response(
            call["url"],
            500,
            "Traceback (most recent call last):\n  File 'app.py', line 3",
        )
    )

    findings = await VerboseErrorCheck().run(context)

    assert len(findings) == 1
    assert findings[0].severity is Severity.MEDIUM


@pytest.mark.asyncio()
async def test_cors_wildcard_is_low_and_reflected_credentials_is_high() -> None:
    _, wildcard = scripted(
        lambda **call: response(
            call["url"], headers={"Access-Control-Allow-Origin": "*"}, body="ok"
        )
    )
    _, reflected = scripted(
        lambda **call: response(
            call["url"],
            headers={
                "Access-Control-Allow-Origin": "https://aegisx-probe.invalid",
                "Access-Control-Allow-Credentials": "true",
            },
            body="ok",
        )
    )

    wildcard_findings = await CORSCheck().run(wildcard)
    reflected_findings = await CORSCheck().run(reflected)

    assert wildcard_findings[0].severity is Severity.LOW
    assert "wildcard" in wildcard_findings[0].title
    assert reflected_findings[0].severity is Severity.HIGH
    assert "credentials" in reflected_findings[0].title


@pytest.mark.asyncio()
async def test_a_response_without_cors_headers_is_not_a_finding() -> None:
    _, context = scripted(lambda **call: response(call["url"], body="ok"))

    assert await CORSCheck().run(context) == []


# --------------------------------------------------------------------------
# API surface and tokens
# --------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_an_admin_panel_is_reported_and_a_soft_200_is_not() -> None:
    def handler(url: str, **_: Any) -> Response:
        if url.endswith("/admin"):
            return response(url, body="<h1>Admin dashboard</h1>")
        if url.endswith("/actuator/env"):
            return response(url, body="management endpoints: env")
        if url.endswith("/metrics"):
            return response(url, body="welcome to our site")
        return response(url, 404, "not found")

    _, context = scripted(handler)

    findings = await AdminSurfaceCheck().run(context)
    found = titles(findings)

    assert "Exposed administration panel at /admin" in found
    assert any("/actuator/env" in title for title in found)
    assert not any("/metrics" in title for title in found)
    actuator = next(finding for finding in findings if "/actuator/env" in finding.title)
    assert actuator.severity is Severity.HIGH


@pytest.mark.asyncio()
async def test_graphql_introspection_is_reported_once() -> None:
    def handler(url: str, **_: Any) -> Response:
        if url.endswith("/graphql"):
            return response(url, body='{"data":{"__schema":{"queryType":{"name":"Query"}}}}')
        return response(url, 404, "")

    _, context = scripted(handler)

    findings = await GraphQLCheck().run(context)

    assert len(findings) == 1
    assert findings[0].severity is Severity.LOW
    assert "__schema" in findings[0].evidence[-1].snippet


def _jwt(header: dict[str, Any], payload: dict[str, Any], signature: str = "c2lnbmF0dXJl") -> str:
    def encode(part: dict[str, Any]) -> str:
        raw = json.dumps(part).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{encode(header)}.{encode(payload)}.{signature}"


@pytest.mark.asyncio()
async def test_a_jwt_with_no_algorithm_and_no_expiry_trips_every_token_rule() -> None:
    token = _jwt(
        {"alg": "none", "typ": "JWT"},
        {"role": "admin", "email": "ops@example.com"},
    )
    _, context = scripted(lambda **call: response(call["url"], body=f"<script>{token}</script>"))

    findings = await TokenCheck().run(context)
    found = titles(findings)

    assert "JWT accepted with the 'none' algorithm" in found
    assert "JWT has no expiry claim" in found
    assert "JWT carries authorisation decisions the client can edit" in found
    assert "JWT contains personal data" in found
    none_finding = next(finding for finding in findings if "'none'" in finding.title)
    assert none_finding.severity is Severity.CRITICAL


@pytest.mark.asyncio()
async def test_a_credential_in_the_query_string_is_reported() -> None:
    url = f"{BASE}/reset?token=abcdefghijklmnop"
    prober = FakeProber(lambda **call: response(call["url"], body="ok"))
    context = CheckContext(
        base_url=url,
        host="app.example.com",
        prober=prober,  # type: ignore[arg-type]
    )

    findings = await TokenCheck().run(context)

    assert any("URL parameter 'token'" in title for title in titles(findings))


@pytest.mark.asyncio()
async def test_a_numeric_session_cookie_is_flagged_as_low_entropy() -> None:
    _, context = scripted(
        lambda **call: response(call["url"], cookies=["session=123456789012"], body="ok")
    )

    findings = await TokenCheck().run(context)

    assert len(findings) == 1
    assert findings[0].confidence is Confidence.PROBABLE
    assert "low-entropy" in findings[0].title


@pytest.mark.asyncio()
async def test_a_signed_jwt_with_a_short_expiry_and_minimal_claims_is_not_flagged() -> None:
    token = _jwt({"alg": "RS256"}, {"sub": "user-1", "exp": 1_900_000_000})
    _, context = scripted(lambda **call: response(call["url"], body=token))

    assert await TokenCheck().run(context) == []


# --------------------------------------------------------------------------
# Injection
# --------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_a_database_error_after_a_quote_proves_sql_injection() -> None:
    def handler(url: str, **_: Any) -> Response:
        if "'" in query(url).get("id", ""):
            return response(url, 500, "You have an error in your SQL syntax near '''")
        return response(url, body="item 1")

    prober, context = scripted(handler)
    context.endpoints = [f"{BASE}/item?id=1"]

    findings = await SQLInjectionCheck().run(context)

    assert len(findings) == 1
    assert findings[0].severity is Severity.HIGH
    assert findings[0].confidence is Confidence.CONFIRMED
    assert any("'" in query(call["url"]).get("id", "") for call in prober.calls)


@pytest.mark.asyncio()
async def test_reflected_script_in_html_is_cross_site_scripting() -> None:
    def handler(url: str, **_: Any) -> Response:
        value = query(url).get("q", "")
        return response(url, body=f"<html><body>results for {value}</body></html>")

    _, context = scripted(handler)
    context.endpoints = [f"{BASE}/search?q=hello"]

    findings = await XSSCheck().run(context)

    assert len(findings) == 1
    assert "q" in findings[0].title
    assert findings[0].category is Category.INJECTION


@pytest.mark.asyncio()
async def test_escaped_reflection_is_not_cross_site_scripting() -> None:
    def handler(url: str, **_: Any) -> Response:
        value = query(url).get("q", "")
        escaped = value.replace("<", "&lt;").replace(">", "&gt;")
        return response(url, body=f"<html>results for {escaped}</html>")

    _, context = scripted(handler)
    context.endpoints = [f"{BASE}/search?q=hello"]

    assert await XSSCheck().run(context) == []


@pytest.mark.asyncio()
async def test_a_template_expression_evaluated_server_side_is_ssti() -> None:
    def handler(url: str, **_: Any) -> Response:
        value = query(url).get("name", "").replace("{{7*7}}", "49")
        return response(url, body=f"<html>hello {value}</html>")

    _, context = scripted(handler)
    context.endpoints = [f"{BASE}/greet?name=bob"]

    findings = await TemplateInjectionCheck().run(context)

    assert len(findings) == 1
    assert findings[0].severity is Severity.CRITICAL
    assert "template engine" in findings[0].description


@pytest.mark.asyncio()
async def test_a_passwd_file_read_through_a_parameter_is_path_traversal() -> None:
    def handler(url: str, **_: Any) -> Response:
        if "etc/passwd" in query(url).get("file", ""):
            return response(url, body="root:x:0:0:root:/root:/bin/bash\n")
        return response(url, 404, "")

    _, context = scripted(handler)
    context.endpoints = [f"{BASE}/download?file=report.pdf"]

    findings = await PathTraversalCheck().run(context)

    assert len(findings) == 1
    assert findings[0].severity is Severity.HIGH


@pytest.mark.asyncio()
async def test_an_echoed_command_marker_proves_command_injection() -> None:
    def handler(url: str, **_: Any) -> Response:
        value = query(url).get("host", "")
        if ";echo " in value:
            return response(url, body=value.split(";echo ", 1)[1])
        return response(url, body="pong")

    _, context = scripted(handler)
    context.endpoints = [f"{BASE}/ping?host=127.0.0.1"]

    findings = await CommandInjectionCheck().run(context)

    assert len(findings) == 1
    assert findings[0].severity is Severity.CRITICAL
    assert "command injection" in findings[0].title


@pytest.mark.asyncio()
async def test_a_login_that_accepts_a_query_operator_is_nosql_injection() -> None:
    def handler(url: str, **_: Any) -> Response:
        value = query(url).get("username", "")
        if value.startswith("{"):
            return response(url, 200, "welcome back, admin")
        return response(url, 401, "invalid credentials")

    _, context = scripted(handler)
    context.endpoints = [f"{BASE}/login?username=bob"]

    findings = await NoSQLInjectionCheck().run(context)

    assert len(findings) == 1
    assert findings[0].severity is Severity.CRITICAL
    assert findings[0].confidence is Confidence.CONFIRMED


@pytest.mark.asyncio()
async def test_an_expanded_internal_entity_is_xxe() -> None:
    def handler(url: str, method: str = "GET", data: Any = None, **_: Any) -> Response:
        text = str(data or "")
        if '<!ENTITY xxe "' in text:
            marker = text.split('<!ENTITY xxe "', 1)[1].split('"', 1)[0]
            return response(url, body=f"<probe>{marker}</probe>")
        return response(url, 400, "bad request")

    _, context = scripted(handler)
    context.endpoints = [f"{BASE}/api/xml"]

    findings = await XXECheck().run(context)

    assert len(findings) == 1
    assert findings[0].severity is Severity.CRITICAL
    assert "entity expansion" in findings[0].description


# --------------------------------------------------------------------------
# Access control and transport
# --------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_swapping_an_object_identifier_for_its_neighbour_is_an_idor() -> None:
    def handler(url: str, **_: Any) -> Response:
        if url.endswith("/api/users/1"):
            return response(url, body='{"id":1,"email":"a@example.com"}')
        if url.endswith("/api/users/2"):
            return response(url, body='{"id":2,"email":"b@example.com"}')
        return response(url, 404, "")

    _, context = scripted(handler)
    context.endpoints = [f"{BASE}/api/users/1"]

    findings = await IDORCheck().run(context)

    assert len(findings) == 1
    assert findings[0].confidence is Confidence.TENTATIVE
    assert findings[0].severity is Severity.MEDIUM


@pytest.mark.asyncio()
async def test_a_denial_on_the_neighbouring_identifier_is_not_an_idor() -> None:
    def handler(url: str, **_: Any) -> Response:
        if url.endswith("/api/users/1"):
            return response(url, body='{"id":1}')
        return response(url, 403, "forbidden")

    _, context = scripted(handler)
    context.endpoints = [f"{BASE}/api/users/1"]

    assert await IDORCheck().run(context) == []


@pytest.mark.asyncio()
async def test_an_accepting_write_method_is_reported() -> None:
    def handler(url: str, method: str = "GET", **_: Any) -> Response:
        if method in ("PUT", "TRACE"):
            return response(url, 200, "ok")
        return response(url, 405, "not allowed")

    _, context = scripted(handler)

    findings = await HTTPMethodCheck().run(context)

    assert sorted(finding.title for finding in findings) == [
        "HTTP method PUT is enabled",
        "HTTP method TRACE is enabled",
    ]


@pytest.mark.asyncio()
async def test_a_redirect_to_the_probe_host_is_an_open_redirect() -> None:
    def handler(url: str, **_: Any) -> Response:
        return response(
            url, 302, "", headers={"Location": "https://aegisx-probe.invalid/x"}
        )

    _, context = scripted(handler)
    context.endpoints = [f"{BASE}/login?next=/home"]

    findings = await OpenRedirectCheck().run(context)

    assert len(findings) == 1
    assert findings[0].severity is Severity.MEDIUM
    assert "next" in findings[0].title


@pytest.mark.asyncio()
async def test_plain_http_is_a_transport_finding() -> None:
    prober = FakeProber(lambda **call: response(call["url"], body="ok"))
    context = CheckContext(
        base_url="http://app.example.com",
        host="app.example.com",
        prober=prober,  # type: ignore[arg-type]
    )

    findings = await TransportCheck().run(context)

    assert titles(findings) == ["Target does not use HTTPS"]


@pytest.mark.asyncio()
async def test_hsts_without_a_max_age_is_reported_as_informational() -> None:
    _, context = scripted(
        lambda **call: response(
            call["url"], headers={"Strict-Transport-Security": "includeSubDomains"}, body="ok"
        )
    )

    findings = await TransportCheck().run(context)

    assert len(findings) == 1
    assert findings[0].severity is Severity.INFO


@pytest.mark.asyncio()
async def test_a_strong_hsts_policy_passes() -> None:
    _, context = scripted(
        lambda **call: response(
            call["url"],
            headers={"Strict-Transport-Security": "max-age=31536000; includeSubDomains"},
            body="ok",
        )
    )

    assert await TransportCheck().run(context) == []


# --------------------------------------------------------------------------
# Protocol and SSRF
# --------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_a_smuggled_response_header_is_crlf_injection() -> None:
    def handler(url: str, **_: Any) -> Response:
        return response(url, headers={"x-aegisx-injected": "aegisx-crlf-marker"}, body="ok")

    _, context = scripted(handler)
    context.endpoints = [f"{BASE}/search?q=hello"]

    findings = await CRLFInjectionCheck().run(context)

    assert len(findings) == 1
    assert findings[0].severity is Severity.HIGH
    assert "CRLF" in findings[0].title


@pytest.mark.asyncio()
async def test_a_reflected_probe_host_is_host_header_injection() -> None:
    def handler(url: str, headers: dict[str, str] | None = None, **_: Any) -> Response:
        host = (headers or {}).get("Host", "")
        return response(url, body=f"<a href='https://{host}/login'>login</a>")

    _, context = scripted(handler)

    findings = await HostHeaderCheck().run(context)

    assert len(findings) == 1
    assert "Host header injection" in findings[0].title


@pytest.mark.asyncio()
async def test_a_server_error_naming_the_loopback_target_is_ssrf() -> None:
    def handler(url: str, **_: Any) -> Response:
        target = query(url).get("url", "")
        if "127.0.0.1" in target:
            return response(url, 500, "Connection refused while contacting 127.0.0.1")
        return response(url, body="fetched")

    _, context = scripted(handler)
    context.endpoints = [f"{BASE}/fetch?url=https://example.com/page"]

    findings = await SSRFCheck().run(context)

    assert len(findings) == 1
    assert findings[0].severity is Severity.HIGH
    assert "loopback" in findings[0].title


@pytest.mark.asyncio()
async def test_a_benign_fetch_that_never_errors_is_not_ssrf() -> None:
    _, context = scripted(lambda **call: response(call["url"], body="fetched fine"))

    findings = await SSRFCheck().run(context)

    assert findings == []


# --------------------------------------------------------------------------
# DNS and cloud storage
# --------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_a_dangling_heroku_record_is_a_takeover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aegisx_agent.cyber.checks import dns

    monkeypatch.setattr(
        dns.socket,
        "gethostbyname_ex",
        lambda host: ("app.herokuapp.com", [], ["1.2.3.4"]),
    )
    _, context = scripted(lambda **call: response(call["url"], 404, "No such app"))
    context.endpoints = []

    findings = await SubdomainTakeoverCheck().run(context)

    assert len(findings) == 1
    assert findings[0].severity is Severity.HIGH
    assert "Dangling DNS record" in findings[0].title


@pytest.mark.asyncio()
async def test_a_live_provider_page_is_not_a_takeover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aegisx_agent.cyber.checks import dns

    monkeypatch.setattr(
        dns.socket,
        "gethostbyname_ex",
        lambda host: ("app.herokuapp.com", [], ["1.2.3.4"]),
    )
    _, context = scripted(lambda **call: response(call["url"], 200, "Welcome to my app"))
    context.endpoints = []

    assert await SubdomainTakeoverCheck().run(context) == []


@pytest.mark.asyncio()
async def test_a_listing_bucket_is_reported_with_its_provider() -> None:
    prober = FakeProber(
        lambda **call: response(
            call["url"], body="<ListBucketResult><Contents><Key>backup.sql</Key>"
        )
    )
    context = CheckContext(
        base_url="https://mybucket.s3.amazonaws.com",
        host="mybucket.s3.amazonaws.com",
        prober=prober,  # type: ignore[arg-type]
    )

    findings = await CloudStorageCheck().run(context)

    assert len(findings) == 1
    assert findings[0].severity is Severity.HIGH
    assert "Amazon S3" in findings[0].title


@pytest.mark.asyncio()
async def test_a_plain_host_is_out_of_scope_for_the_storage_check() -> None:
    _, context = scripted(lambda **call: response(call["url"], body="<ListBucketResult>"))

    assert await CloudStorageCheck().run(context) == []


# --------------------------------------------------------------------------
# The check framework itself
# --------------------------------------------------------------------------


class ExplodingCheck:
    name = "exploding"
    category = Category.INFO
    cost = "cheap"

    async def run(self, context: CheckContext) -> list[Any]:
        raise RuntimeError("kaboom")


@pytest.mark.asyncio()
async def test_a_broken_check_is_contained_and_recorded() -> None:
    _, context = scripted(lambda **call: response(call["url"], body="ok"))

    findings = await run_check(ExplodingCheck(), context)  # type: ignore[arg-type]

    assert findings == []
    assert any("kaboom" in error for error in context.options["errors"])


@pytest.mark.asyncio()
async def test_run_checks_merges_every_check_that_survived() -> None:
    _, context = scripted(lambda **call: response(call["url"], body="ok"))

    findings = await run_checks([HeaderCheck(), CookieCheck()], context)

    assert findings
    assert all(finding.category in (Category.HEADERS, Category.COOKIE) for finding in findings)
