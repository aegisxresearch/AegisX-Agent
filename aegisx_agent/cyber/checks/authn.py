"""Authentication and session-token checks.

A token is the most common place an authorisation decision leaks out of
the server and into the client's hands. This module looks at the tokens
the target actually issues and asks three questions:

- **Is the token verifiable?** A JWT with ``alg: none`` is accepted by
  nobody's validation code and by everybody's — the signature is not
  checked at all.
- **Does the token expire?** A token with no ``exp`` claim is valid
  forever, so a single leak is permanent.
- **Does the token carry more than it should?** A payload holding
  ``role: admin`` means the client can be trusted for nothing, and a
  payload holding personal data means the token is a copy of a record
  that lives in a browser.

Everything is read-only. No token is replayed against an authenticated
endpoint, because doing so would be using somebody else's session.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from aegisx_agent.cyber.checks import CheckContext, finding
from aegisx_agent.cyber.models import (
    Category,
    Confidence,
    Evidence,
    EvidenceKind,
    Finding,
    Severity,
)

#: Three base64url segments separated by dots.
JWT_PATTERN = re.compile(r"\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")

#: Query parameters that carry a token.
TOKEN_PARAMS: frozenset[str] = frozenset(
    {
        "token",
        "access_token",
        "id_token",
        "refresh_token",
        "jwt",
        "auth",
        "authorization",
        "api_key",
        "apikey",
        "key",
        "session",
        "sessionid",
        "session_id",
        "sid",
        "sso",
        "samlresponse",
        "jwt_token",
        "bearer",
    }
)

#: Payload claims that mean the client is trusted with an authorisation
#: decision it should not hold.
AUTHORISATION_CLAIMS: tuple[str, ...] = (
    "role",
    "roles",
    "admin",
    "is_admin",
    "isadmin",
    "permissions",
    "scope",
    "scopes",
    "groups",
    "privilege",
    "access",
)

#: Payload claims that mean the token carries personal data.
PERSONAL_CLAIMS: tuple[str, ...] = (
    "email",
    "name",
    "phone",
    "ssn",
    "dob",
    "date_of_birth",
    "address",
    "credit_card",
    "national_id",
)


def _decode_segment(segment: str) -> dict[str, Any] | None:
    """Decode one base64url JSON segment of a JWT."""
    padding = "=" * (-len(segment) % 4)
    try:
        raw = base64.urlsafe_b64decode(segment + padding)
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _redact(value: Any) -> str:
    """Render a claim value without exposing anything secret."""
    text = str(value)
    if len(text) > 24:
        return f"{text[:8]}… ({len(text)} chars)"
    return text


@dataclass
class TokenCheck:
    """Audit the tokens a target issues for structural weaknesses.

    The check reads tokens out of responses, cookies, and URLs. It
    never sends a token back to the target: this is a static audit of
    what the server hands out, not an attempt to use a session.
    """

    name: str = "token-audit"
    category: Category = Category.AUTHZ
    cost: str = "cheap"

    async def run(self, context: CheckContext) -> list[Finding]:
        found: list[Finding] = []
        seen: set[str] = set()

        base = await context.fetch(context.base_url)
        if not base.error:
            found.extend(self._audit_response(context, base, seen))
            found.extend(self._audit_cookies(context, base, seen))
            found.extend(self._audit_url_tokens(context, base, seen))

        for endpoint in context.endpoints[:8]:
            if endpoint == context.base_url:
                continue
            response = await context.fetch(endpoint)
            if response.error:
                continue
            found.extend(self._audit_response(context, response, seen))
            found.extend(self._audit_url_tokens(context, response, seen))

        return found

    # === Token sources ================================================= #

    def _audit_response(
        self, context: CheckContext, response: Any, seen: set[str]
    ) -> list[Finding]:
        """Look for tokens in a response body."""
        found: list[Finding] = []
        for match in JWT_PATTERN.finditer(response.body):
            token = match.group(0)
            if token in seen:
                continue
            seen.add(token)
            found.extend(self._audit_jwt(context, token, response.url))
        return found

    def _audit_cookies(self, context: CheckContext, response: Any, seen: set[str]) -> list[Finding]:
        """Look for tokens in the cookies the server sets."""
        found: list[Finding] = []
        for raw_cookie in response.header_values("set-cookie"):
            name, _, value = raw_cookie.partition("=")
            value = value.split(";")[0].strip()
            if not value:
                continue
            if JWT_PATTERN.fullmatch(value):
                if value not in seen:
                    seen.add(value)
                    found.extend(self._audit_jwt(context, value, response.url))
                continue
            if name.strip().lower() in {"session", "sessionid", "sid", "phpsessid", "jsessionid"}:
                found.append(self._weak_session_finding(context, name.strip(), value, response.url))
        return found

    def _audit_url_tokens(
        self, context: CheckContext, response: Any, seen: set[str]
    ) -> list[Finding]:
        """Look for tokens passed in the URL itself."""
        found: list[Finding] = []
        parsed = urlparse(response.url)
        for name, values in parse_qs(parsed.query, keep_blank_values=True).items():
            if name.lower() not in TOKEN_PARAMS:
                continue
            value = values[0] if values else ""
            if len(value) < 12:
                continue
            found.append(
                finding(
                    title=f"Credential passed in the URL parameter '{name}'",
                    severity=Severity.MEDIUM,
                    category=Category.AUTHZ,
                    confidence=Confidence.CONFIRMED,
                    context=context,
                    endpoint=response.url,
                    description=(
                        f"The URL carries a credential in the '{name}' parameter. "
                        "URLs are written to access logs, kept in browser history, "
                        "and sent in the Referer header to every third-party host "
                        "the page loads."
                    ),
                    impact=(
                        "Anyone with access to the log file, the proxy, the browser "
                        "history, or the referring server learns the credential."
                    ),
                    remediation=(
                        "Pass tokens in an Authorization header or a cookie marked "
                        "HttpOnly, and scrub access logs of query strings."
                    ),
                    cwe="CWE-598",
                    owasp="A07:2021 - Identification and Authentication Failures",
                    cvss=5.3,
                    references=["https://cwe.mitre.org/data/definitions/598.html"],
                    evidence=[
                        Evidence(
                            kind=EvidenceKind.REQUEST,
                            summary=f"Credential observed in the '{name}' query parameter",
                            request=response.request_line(),
                            response_status=response.status,
                        )
                    ],
                    source=self.name,
                    tags=["token", "leak", name],
                )
            )
        return found

    # === The audit ===================================================== #

    def _audit_jwt(self, context: CheckContext, token: str, endpoint: str) -> list[Finding]:
        """Audit one JWT's header and payload."""
        found: list[Finding] = []
        header_segment, payload_segment, _signature = token.split(".", 2)
        header = _decode_segment(header_segment)
        payload = _decode_segment(payload_segment)
        if header is None or payload is None:
            return found

        algorithm = str(header.get("alg", ""))
        if algorithm.lower() == "none":
            found.append(
                self._jwt_finding(
                    context,
                    endpoint,
                    token,
                    header,
                    payload,
                    title="JWT accepted with the 'none' algorithm",
                    severity=Severity.CRITICAL,
                    description=(
                        "The token declares alg: none, so its signature carries no "
                        "meaning and a validation routine that trusts the header "
                        "instead of pinning an algorithm will accept a token the "
                        "attacker signed themselves."
                    ),
                    impact=(
                        "An attacker forges a token for any identity they choose — "
                        "including an administrative one — and the server accepts "
                        "it as genuine."
                    ),
                    remediation=(
                        "Reject tokens whose alg is 'none', and pin the expected "
                        "algorithm on the server instead of reading it from the "
                        "token. Verify the signature against the key you trust."
                    ),
                    cwe="CWE-347",
                    cvss=9.8,
                    tags=["jwt", "auth-bypass"],
                )
            )

        if "exp" not in payload:
            found.append(
                self._jwt_finding(
                    context,
                    endpoint,
                    token,
                    header,
                    payload,
                    title="JWT has no expiry claim",
                    severity=Severity.MEDIUM,
                    description=(
                        "The token carries no 'exp' claim, so nothing in it ever "
                        "expires. A token that leaks stays usable for as long as "
                        "the signing key lives."
                    ),
                    impact=(
                        "One leaked token is a permanent credential. Revoking a "
                        "session means rotating the signing key for everyone."
                    ),
                    remediation=(
                        "Issue tokens with a short expiry and a refresh token, and "
                        "reject tokens without 'exp' on the server."
                    ),
                    cwe="CWE-613",
                    cvss=6.5,
                    tags=["jwt", "session"],
                )
            )

        authorisation_claims = sorted(key for key in payload if key.lower() in AUTHORISATION_CLAIMS)
        if authorisation_claims:
            found.append(
                self._jwt_finding(
                    context,
                    endpoint,
                    token,
                    header,
                    payload,
                    title="JWT carries authorisation decisions the client can edit",
                    severity=Severity.MEDIUM,
                    description=(
                        "The token payload carries "
                        + ", ".join(f"'{claim}'" for claim in authorisation_claims)
                        + ". A signed token is opaque to its holder, but a "
                        "client-side claim that the server then trusts is an "
                        "authorisation decision living where the user can reach it."
                    ),
                    impact=(
                        "If any part of the payload is decoded, re-encoded, and "
                        "re-signed with a guessable or leaked key, the attacker "
                        "raises their own privileges without needing another flaw."
                    ),
                    remediation=(
                        "Keep authorisation decisions on the server. The token "
                        "should identify the caller; it should not tell the "
                        "server what the caller is allowed to do."
                    ),
                    cwe="CWE-602",
                    cvss=6.5,
                    tags=["jwt", "authorization"],
                )
            )

        personal_claims = sorted(key for key in payload if key.lower() in PERSONAL_CLAIMS)
        if personal_claims:
            found.append(
                self._jwt_finding(
                    context,
                    endpoint,
                    token,
                    header,
                    payload,
                    title="JWT contains personal data",
                    severity=Severity.LOW,
                    description=(
                        "The token payload carries "
                        + ", ".join(f"'{claim}'" for claim in personal_claims)
                        + ". The token is stored in the browser, so the personal "
                        "data travels with it, is readable by any script on the "
                        "origin, and ends up in logs that record the header."
                    ),
                    impact=(
                        "Personal data leaves the database and lands in places it "
                        "was never meant to reach: browser storage, proxy logs, "
                        "and crash reports."
                    ),
                    remediation=(
                        "Keep the token to an opaque identifier or a minimal "
                        "subject claim, and fetch personal data from an "
                        "authenticated endpoint instead of embedding it."
                    ),
                    cwe="CWE-359",
                    cvss=3.7,
                    tags=["jwt", "privacy"],
                )
            )

        return found

    def _jwt_finding(
        self,
        context: CheckContext,
        endpoint: str,
        token: str,
        header: dict[str, Any],
        payload: dict[str, Any],
        *,
        title: str,
        severity: Severity,
        description: str,
        impact: str,
        remediation: str,
        cwe: str,
        cvss: float,
        tags: list[str],
    ) -> Finding:
        """Build a finding about a JWT, with the header and payload as evidence."""
        return finding(
            title=title,
            severity=severity,
            category=self.category,
            confidence=Confidence.CONFIRMED,
            context=context,
            endpoint=endpoint,
            description=description,
            impact=impact,
            remediation=remediation,
            cwe=cwe,
            owasp="A07:2021 - Identification and Authentication Failures",
            cvss=cvss,
            references=["https://cwe.mitre.org/data/definitions/347.html"],
            evidence=[
                Evidence(
                    kind=EvidenceKind.MATCH,
                    summary="Decoded JWT header and payload",
                    snippet=(
                        "header: "
                        + json.dumps(header, ensure_ascii=False)
                        + "\npayload: "
                        + json.dumps(
                            {key: _redact(value) for key, value in payload.items()},
                            ensure_ascii=False,
                        )
                    ),
                )
            ],
            source=self.name,
            tags=tags,
        )

    def _weak_session_finding(
        self, context: CheckContext, name: str, value: str, endpoint: str
    ) -> Finding:
        """Report a session cookie whose value looks predictable."""
        entropy_note = (
            "The value is short and looks like a counter or a timestamp rather "
            "than a random identifier, which makes it guessable."
            if len(value) <= 12 and value.isdigit()
            else "The value is short; short session identifiers are guessable."
        )
        return finding(
            title=f"Session cookie '{name}' has a low-entropy value",
            severity=Severity.MEDIUM,
            category=self.category,
            confidence=Confidence.PROBABLE,
            context=context,
            endpoint=endpoint,
            description=(
                f"The session cookie '{name}' carries a {len(value)}-character "
                f"value. {entropy_note}"
            ),
            impact=(
                "An attacker who guesses or enumerates the session identifier "
                "takes over the session, with no password and no second factor."
            ),
            remediation=(
                "Generate session identifiers with a cryptographically secure "
                "random source and at least 128 bits of entropy, and rotate the "
                "identifier on every privilege change."
            ),
            cwe="CWE-330",
            owasp="A07:2021 - Identification and Authentication Failures",
            cvss=6.5,
            evidence=[
                Evidence(
                    kind=EvidenceKind.MATCH,
                    summary=f"Cookie '{name}' set with a {len(value)}-character value",
                    request=endpoint,
                )
            ],
            source=self.name,
            tags=["session", "entropy"],
        )
