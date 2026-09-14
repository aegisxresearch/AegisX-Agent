"""Browser plugin — fetch and read web pages as plain text.

Loaded explicitly:

    agent.load_plugin_module("aegisx_agent.plugins.builtin.browser")

``read_page`` pulls a URL with ``httpx`` and strips HTML down to readable
text. ``http_get`` returns raw JSON/text bodies for API endpoints. Network
calls are exactly the kind of side effect the permission gate exists for, so
the risk is ``CAUTION``: allowed by default, refused in read-only mode.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from socket import gaierror
from urllib.parse import urlparse

import httpx

from aegisx_agent.plugins import PluginManifest, PluginPermissionPolicy, define_plugin
from aegisx_agent.tools.base import ToolRisk

DEFAULT_TIMEOUT = 20.0
MAX_BYTES = 2_000_000
USER_AGENT = "aegisx-agent-browser/1.0"

_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAGS = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def _private_http_allowed() -> bool:
    """Opt-in escape hatch for tests and self-hosted targets."""
    return os.environ.get("AEGISX_ALLOW_PRIVATE_HTTP", "").strip() == "1"


def _assert_public_http(url: str) -> None:
    """Refuse non-HTTP schemes and loopback/private targets (basic SSRF guard).

    This is a guardrail for the agent, not a sandbox: a determined user can
    always reach their own localhost with curl. What it prevents is the model
    being talked into probing internal services or file:// URLs by accident.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"only http/https URLs are allowed, got: {parsed.scheme or 'none'!r}")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError(f"URL has no host: {url}")
    if host in ("localhost",) or host.endswith(".local") or host.endswith(".internal"):
        if not _private_http_allowed():
            raise ValueError(f"refusing to fetch a local address: {host}")
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return  # a hostname — DNS resolution decides; nothing more to check here
    if not _private_http_allowed() and (
        address.is_private or address.is_loopback or address.is_link_local or address.is_reserved
    ):
        raise ValueError(f"refusing to fetch a private address: {host}")


def _fetch(url: str, headers: dict[str, str] | None = None) -> httpx.Response:
    _assert_public_http(url)
    try:
        response = httpx.get(
            url,
            headers={"User-Agent": USER_AGENT, **(headers or {})},
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
        )
        response.raise_for_status()
    except (httpx.HTTPError, gaierror) as exc:
        raise ValueError(f"fetch failed: {type(exc).__name__}: {exc}") from exc
    return response


def _html_to_text(html: str) -> str:
    """Strip a document down to roughly readable plain text."""
    text = _SCRIPT_STYLE.sub(" ", html)
    text = _TAGS.sub("\n", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    text = _WHITESPACE.sub(" ", text)
    text = _BLANK_LINES.sub("\n\n", text)
    return text.strip()


READ_PAGE_MANIFEST = PluginManifest(
    plugin_id="browser",
    version="1.0.0",
    tool_name="read_page",
    description=(
        "Fetch a web page and return its readable text (HTML stripped, first "
        "8000 characters). Use for documentation, articles, and blog posts."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The http(s) URL to read"},
        },
        "required": ["url"],
    },
    risk=ToolRisk.CAUTION,
    permission=PluginPermissionPolicy(allow_in_read_only=False),
)


@define_plugin(READ_PAGE_MANIFEST)
def read_page(url: str) -> str:
    response = _fetch(url)
    content_type = response.headers.get("content-type", "")
    if "html" in content_type:
        text = _html_to_text(response.text)
    else:
        text = response.text
    if not text:
        return "The page returned no readable text."
    if len(text) > 8000:
        text = text[:8000] + "\n\n… (truncated at 8000 characters)"
    return text


HTTP_GET_MANIFEST = PluginManifest(
    plugin_id="browser",
    version="1.0.0",
    tool_name="http_get",
    description=(
        "Perform an HTTP GET against an API endpoint and return the raw body "
        "(pretty-printed when the response is JSON). Use for REST APIs."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The http(s) URL to request"},
        },
        "required": ["url"],
    },
    risk=ToolRisk.CAUTION,
    permission=PluginPermissionPolicy(allow_in_read_only=False),
)


@define_plugin(HTTP_GET_MANIFEST)
def http_get(url: str) -> str:
    response = _fetch(url)
    body = response.text[:MAX_BYTES]
    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        try:
            return json.dumps(json.loads(body), indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            pass
    return body or "(empty response body)"


PLUGINS = (read_page, http_get)
