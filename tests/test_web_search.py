"""WebSearchTool over httpx.MockTransport — parser and fallback, no network.

The DuckDuckGo HTML parser is fed realistic result markup, the Instant
Answers fallback is served JSON, and the branch/failure behaviour is
exercised end to end.
"""

from __future__ import annotations

import json

import httpx
import pytest
from support import run

from aegisx_agent.tools.base import ToolStatus
from aegisx_agent.tools.web_search import WebSearchTool

DDG_HTML = """
<html><body>
<div class="result">
  <a class="result__a" href="https://docs.example.com/rate-limits">Rate limits</a>
  <a class="result__snippet" href="https://docs.example.com/rate-limits">60 requests per minute.</a>
</div>
<div class="result">
  <a class="result__a" href="https://docs.example.com/auth">Authentication</a>
  <a class="result__snippet" href="https://docs.example.com/auth">Use an API key.</a>
</div>
</body></html>
"""

DDG_API = {
    "Heading": "rate limit",
    "AbstractText": "The API allows 60 requests per minute.",
    "AbstractURL": "https://api.example.com/docs",
    "RelatedTopics": [
        {"Text": "Quotas reset hourly.", "FirstURL": "https://api.example.com/quotas"},
        {"NotATopic": True},
        ["a", "list", "is", "not", "a", "dict"],
    ],
}


@pytest.fixture()
def html_only() -> httpx.MockTransport:
    """HTML search succeeds; the API fallback should never be reached."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert "html.duckduckgo.com" in str(request.url)
        return httpx.Response(200, text=DDG_HTML)

    return httpx.MockTransport(handler)


def test_html_results_are_parsed_with_titles_urls_snippets(html_only) -> None:
    tool = WebSearchTool(transport=html_only)

    result = run(tool.execute(query="api rate limit", num_results=5))

    assert result.status is ToolStatus.SUCCESS
    assert "Search results for: api rate limit" in result.output
    assert "Rate limits" in result.output
    assert "https://docs.example.com/rate-limits" in result.output
    assert "60 requests per minute." in result.output
    assert "Authentication" in result.output


def test_num_results_caps_the_parsed_list(html_only) -> None:
    tool = WebSearchTool(transport=html_only)

    result = run(tool.execute(query="api rate limit", num_results=1))

    assert result.status is ToolStatus.SUCCESS
    assert "Authentication" not in result.output


def test_empty_html_falls_back_to_the_instant_answers_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "api.duckduckgo.com" in str(request.url):
            return httpx.Response(200, text=json.dumps(DDG_API))
        return httpx.Response(200, text="<html><body>no results</body></html>")

    tool = WebSearchTool(transport=httpx.MockTransport(handler))

    result = run(tool.execute(query="rate limit"))

    assert result.status is ToolStatus.SUCCESS
    assert "The API allows 60 requests per minute." in result.output
    assert "Quotas reset hourly." in result.output
    # Non-dict topics must be skipped, not crash.
    assert "NotATopic" not in result.output


def test_nothing_anywhere_still_succeeds_with_a_polite_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "api.duckduckgo.com" in str(request.url):
            return httpx.Response(200, text=json.dumps({}))
        return httpx.Response(200, text="<html></html>")

    tool = WebSearchTool(transport=httpx.MockTransport(handler))

    result = run(tool.execute(query="obscure thing"))

    assert result.status is ToolStatus.SUCCESS
    assert "No results found for: obscure thing" in result.output


def test_an_empty_query_is_rejected_before_any_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request should be made")

    tool = WebSearchTool(transport=httpx.MockTransport(handler))

    result = run(tool.execute(query="  "))

    assert result.status is ToolStatus.ERROR
    assert "Query is required" in (result.error or "")


def test_a_network_failure_becomes_a_structured_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    tool = WebSearchTool(transport=httpx.MockTransport(handler))

    result = run(tool.execute(query="anything"))

    assert result.status is ToolStatus.ERROR
    assert "Search failed" in (result.error or "")
