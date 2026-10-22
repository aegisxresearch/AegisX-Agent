"""HTTP prober — the only place a scan is allowed to touch the network.

Every request in the cyber layer goes through :class:`Prober`. That is a
deliberate choke point, because the safety properties of an autonomous
scanner all live at the network boundary:

- the scope guard is consulted *before* each request, and again for every
  redirect hop (a redirect to another host does not widen the scope);
- the request budget and rate limit are applied here, so no individual
  check can bypass them;
- responses are size-capped, because a hostile or broken target can
  stream gigabytes;
- the request and response are captured as evidence, so every finding can
  point at what actually came back.

The prober never raises on a network failure. A scan that dies because
one endpoint timed out is a scan that reports nothing.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from aegisx_agent.cyber.scope import ScopeError, ScopeGuard, ScopePolicy


@dataclass
class Response:
    """One HTTP exchange, captured in full enough to be evidence."""

    url: str
    status: int = 0
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    elapsed_ms: int = 0
    error: str = ""
    redirects: list[str] = field(default_factory=list)
    truncated: bool = False
    content_type: str = ""
    #: Repeated headers (``Set-Cookie``) keep every value; the flat
    #: ``headers`` mapping keeps only the first for convenience.
    multi_headers: dict[str, list[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error == "" and 200 <= self.status < 400

    @property
    def body_length(self) -> int:
        return len(self.body)

    def header(self, name: str, default: str = "") -> str:
        """Case-insensitive header lookup."""
        return self.headers.get(name.lower(), default)

    def header_values(self, name: str) -> list[str]:
        """Every value for a repeated header (``Set-Cookie`` and friends)."""
        return list(self.multi_headers.get(name.lower(), []))

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "status": self.status,
            "method": self.method,
            "headers": dict(self.headers),
            "body_length": self.body_length,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "redirects": list(self.redirects),
            "content_type": self.content_type,
        }

    def request_line(self) -> str:
        """The request as it would appear in an HTTP log."""
        return f"{self.method} {self.url} HTTP/1.1"


class Prober:
    """Scoped, rate-limited HTTP client with evidence capture."""

    #: Bodies larger than this are truncated. A security scan reads
    #: headers and markers, not megabytes of HTML.
    MAX_BODY_BYTES = 512 * 1024

    def __init__(
        self,
        guard: ScopeGuard,
        *,
        policy: ScopePolicy | None = None,
        timeout: float = 15.0,
        verify_tls: bool = False,
        max_redirects: int = 5,
    ) -> None:
        self.guard = guard
        self.policy = policy or guard.policy
        self.timeout = timeout
        self.verify_tls = verify_tls
        self.max_redirects = max_redirects
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            verify=verify_tls,
            follow_redirects=False,  # redirects are followed manually, scope-checked
            headers={
                "User-Agent": self.policy.user_agent,
                "Accept": "*/*",
            },
        )
        self._semaphore = asyncio.Semaphore(self.policy.max_concurrency)
        self.requests_sent = 0
        self.errors: list[str] = []

    async def __aenter__(self) -> Prober:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    # === The request path ============================================= #

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
        """Perform one scoped request and capture the exchange.

        Returns a :class:`Response` in all cases, including scope
        violations (as an error response) and transport failures.
        """
        follow = self.policy.follow_redirects if follow_redirects is None else follow_redirects
        current_url = url
        redirects: list[str] = []

        async with self._semaphore:
            for hop in range(self.max_redirects + 1):
                try:
                    self.guard.ensure_allowed(current_url, method=method)
                except ScopeError as exc:
                    # The redirect chain is kept: a refusal is evidence too,
                    # and the hop that was refused is what explains it.
                    return Response(
                        url=current_url,
                        method=method,
                        error=f"scope violation: {exc.violation.value} ({exc.detail})",
                        redirects=redirects,
                    )

                await self.guard.throttle()
                started = time.monotonic()
                try:
                    raw = await self._client.request(
                        method,
                        current_url,
                        headers=headers,
                        content=data,
                        json=json_body,
                        params=params,
                        timeout=timeout or self.timeout,
                    )
                except httpx.HTTPError as exc:
                    elapsed = int((time.monotonic() - started) * 1000)
                    self.errors.append(f"{current_url}: {type(exc).__name__}")
                    return Response(
                        url=current_url,
                        method=method,
                        error=f"{type(exc).__name__}: {exc}",
                        elapsed_ms=elapsed,
                        redirects=redirects,
                    )

                elapsed = int((time.monotonic() - started) * 1000)
                self.guard.record_request(current_url)
                self.requests_sent += 1

                body, truncated = self._read_body(raw)
                response = Response(
                    url=current_url,
                    status=raw.status_code,
                    method=method,
                    headers={key.lower(): value for key, value in raw.headers.items()},
                    body=body,
                    elapsed_ms=elapsed,
                    redirects=redirects,
                    truncated=truncated,
                    content_type=raw.headers.get("content-type", ""),
                    multi_headers=self._collect_multi_headers(raw),
                )

                if not follow or raw.status_code not in (301, 302, 303, 307, 308):
                    return response
                location = raw.headers.get("location")
                if not location:
                    return response
                if hop >= self.max_redirects:
                    response.error = f"too many redirects (> {self.max_redirects})"
                    return response

                next_url = urljoin(current_url, location)
                redirects.append(next_url)
                current_url = next_url

            return Response(url=current_url, method=method, error="redirect loop")

    @staticmethod
    def _collect_multi_headers(raw: httpx.Response) -> dict[str, list[str]]:
        """Group repeated headers by lowercase name."""
        grouped: dict[str, list[str]] = {}
        for key, value in raw.headers.multi_items():
            grouped.setdefault(key.lower(), []).append(value)
        return grouped

    def _read_body(self, raw: httpx.Response) -> tuple[str, bool]:
        """Read a response body, capped in size."""
        try:
            content = raw.content[: self.MAX_BODY_BYTES]
        except (httpx.HTTPError, ValueError):
            return "", False
        truncated = len(raw.content) > self.MAX_BODY_BYTES
        try:
            return content.decode(raw.encoding or "utf-8", errors="replace"), truncated
        except (LookupError, UnicodeError):
            return content.decode("utf-8", errors="replace"), truncated

    async def head_or_get(self, url: str, **kwargs: Any) -> Response:
        """HEAD, falling back to GET when the server rejects it."""
        response = await self.request(url, method="HEAD", **kwargs)
        if response.error or response.status in (403, 405, 501):
            return await self.request(url, method="GET", **kwargs)
        return response

    async def probe_many(
        self, urls: list[str], *, method: str = "GET", limit: int | None = None
    ) -> list[Response]:
        """Probe several URLs concurrently, respecting the concurrency cap."""
        selected = urls[:limit] if limit else urls
        results = await asyncio.gather(
            *(self.request(url, method=method) for url in selected), return_exceptions=True
        )
        responses: list[Response] = []
        for url, outcome in zip(selected, results, strict=False):
            if isinstance(outcome, Response):
                responses.append(outcome)
            else:
                responses.append(Response(url=url, method=method, error=f"probe failed: {outcome}"))
        return responses

    # === Convenience wrappers ========================================= #

    @staticmethod
    def same_origin(first: str, second: str) -> bool:
        """Whether two URLs share scheme, host, and port."""
        left = urlparse(first)
        right = urlparse(second)
        return (
            left.scheme == right.scheme
            and left.hostname == right.hostname
            and (left.port or 80) == (right.port or 80)
        )
