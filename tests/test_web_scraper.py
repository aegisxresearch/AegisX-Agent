"""WebScraperTool over httpx.MockTransport — the whole pipeline, no network.

The fake server serves a realistic HTML page; every extraction mode, the
URL-scheme normalisation, the error paths, and truncation are exercised for
real against it.
"""

from __future__ import annotations

import httpx
import pytest
from support import run

from aegisx_agent.tools.base import ToolStatus
from aegisx_agent.tools.web_scraper import WebScraperTool

PAGE = """
<html>
  <head>
    <title>Docs Home</title>
    <meta name="description" content="All about the widget API">
    <meta property="og:title" content="Widget Docs">
  </head>
  <body>
    <nav>skip me</nav>
    <script>var secret = "not-content";</script>
    <style>.hidden { color: red }</style>
    <h1>Widget API</h1>
    <p>The rate limit is 60 requests per minute &amp; climbing.</p>
    <a href="/guide">Guide</a>
    <a href="https://example.com/other">Other</a>
    <a href="javascript:void(0)">nope</a>
    <a href="mailto:x@y.z">mail</a>
    <img src="/img/logo.png">
    <img src="https://cdn.example.com/banner.png">
  </body>
</html>
"""


@pytest.fixture()
def transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/500":
            return httpx.Response(500, text="boom")
        return httpx.Response(200, text=PAGE)

    return httpx.MockTransport(handler)


def _tool(transport: httpx.MockTransport) -> WebScraperTool:
    return WebScraperTool(transport=transport)


def test_text_extraction_strips_chrome_and_decodes_entities(transport) -> None:
    result = run(_tool(transport).execute(url="https://docs.example.com/"))

    assert result.status is ToolStatus.SUCCESS
    assert "Widget API" in result.output
    assert "rate limit is 60 requests per minute & climbing" in result.output
    # script/style/nav content must not survive
    assert "not-content" not in result.output
    assert "skip me" not in result.output
    assert result.metadata["url"] == "https://docs.example.com/"


def test_a_bare_hostname_is_upgraded_to_https(transport) -> None:
    result = run(_tool(transport).execute(url="docs.example.com"))

    assert result.status is ToolStatus.SUCCESS
    assert result.metadata["url"] == "https://docs.example.com"


def test_link_extraction_makes_urls_absolute_and_filters_junk(transport) -> None:
    result = run(_tool(transport).execute(url="https://docs.example.com/", extract="links"))

    assert result.status is ToolStatus.SUCCESS
    assert "https://docs.example.com/guide" in result.output
    assert "https://example.com/other" in result.output
    assert "javascript:" not in result.output
    assert "mailto:" not in result.output
    assert "Total: 2 unique links" in result.output


def test_image_extraction(transport) -> None:
    result = run(_tool(transport).execute(url="https://docs.example.com/", extract="images"))

    assert result.status is ToolStatus.SUCCESS
    assert "https://docs.example.com/img/logo.png" in result.output
    assert "https://cdn.example.com/banner.png" in result.output
    assert "Total: 2 images" in result.output


def test_meta_extraction_pulls_title_and_tags(transport) -> None:
    result = run(_tool(transport).execute(url="https://docs.example.com/", extract="meta"))

    assert result.status is ToolStatus.SUCCESS
    assert "title: Docs Home" in result.output
    assert "description: All about the widget API" in result.output
    assert "og:title: Widget Docs" in result.output


def test_html_extraction_returns_raw_markup(transport) -> None:
    result = run(_tool(transport).execute(url="https://docs.example.com/", extract="html"))

    assert result.status is ToolStatus.SUCCESS
    assert "<h1>Widget API</h1>" in result.output


def test_all_mode_combines_meta_text_and_links(transport) -> None:
    result = run(_tool(transport).execute(url="https://docs.example.com/", extract="all"))

    assert result.status is ToolStatus.SUCCESS
    assert "Full Extraction" in result.output
    assert "Widget API" in result.output
    assert "Links from" in result.output


def test_a_missing_url_is_rejected_without_any_request(transport) -> None:
    result = run(_tool(transport).execute(url=""))

    assert result.status is ToolStatus.ERROR
    assert "URL is required" in (result.error or "")


def test_an_http_error_becomes_a_structured_failure(transport) -> None:
    result = run(_tool(transport).execute(url="https://docs.example.com/500"))

    assert result.status is ToolStatus.ERROR
    assert "HTTP 500" in (result.error or "")


def test_max_length_truncates_text(transport) -> None:
    result = run(_tool(transport).execute(url="https://docs.example.com/", max_length=40))

    assert result.status is ToolStatus.SUCCESS
    assert "[truncated]" in result.output
