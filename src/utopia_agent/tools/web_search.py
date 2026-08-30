"""Web search tool — searches the internet using DuckDuckGo."""

from __future__ import annotations

from typing import Any

import httpx

from utopia_agent.tools.base import Tool, ToolResult, ToolStatus


class WebSearchTool(Tool):
    """Search the web using DuckDuckGo Instant Answers API + HTML scraping."""

    def __init__(self) -> None:
        super().__init__(
            name="web_search",
            description=(
                "Search the internet for information. Returns relevant search results "
                "with titles, URLs, and snippets. Use this to find current information, "
                "documentation, news, or any web content."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query",
                    },
                    "num_results": {
                        "type": "integer",
                        "description": "Number of results to return (default: 5)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        query = kwargs.get("query", "")
        num_results = kwargs.get("num_results", 5)

        if not query:
            return ToolResult(status=ToolStatus.ERROR, output="", error="Query is required")

        try:
            # Use DuckDuckGo HTML search
            results = await self._search_ddg(query, num_results)
            if not results:
                # Fallback: use DuckDuckGo API
                results = await self._search_ddg_api(query)

            if results:
                output = f"Search results for: {query}\n\n"
                for i, r in enumerate(results, 1):
                    output += f"{i}. {r['title']}\n"
                    output += f"   URL: {r['url']}\n"
                    output += f"   {r.get('snippet', 'No snippet')}\n\n"
                return ToolResult(status=ToolStatus.SUCCESS, output=output.strip())

            return ToolResult(
                status=ToolStatus.SUCCESS,
                output=f"No results found for: {query}",
            )
        except Exception as e:
            return ToolResult(
                status=ToolStatus.ERROR, output="", error=f"Search failed: {e}"
            )

    async def _search_ddg(self, query: str, num: int) -> list[dict[str, str]]:
        """Search via DuckDuckGo HTML."""
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={"User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()

        from html.parser import HTMLParser

        results: list[dict[str, str]] = []

        class DDGParser(HTMLParser):
            def __init__(self) -> None:
                super().__init__()
                self._in_result = False
                self._in_title = False
                self._in_snippet = False
                self._current: dict[str, str] = {}

            def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                attrs_dict = dict(attrs)
                cls = attrs_dict.get("class", "")
                if tag == "a" and "result__a" in cls:
                    self._in_title = True
                    self._current["url"] = attrs_dict.get("href", "")
                    self._current["title"] = ""
                if tag == "a" and "result__snippet" in cls:
                    self._in_snippet = True
                    self._current["snippet"] = ""

            def handle_endtag(self, tag: str) -> None:
                if tag == "a" and self._in_title:
                    self._in_title = False
                if tag == "a" and self._in_snippet:
                    self._in_snippet = False
                    if self._current.get("title"):
                        results.append(dict(self._current))
                    self._current = {}

            def handle_data(self, data: str) -> None:
                if self._in_title:
                    self._current["title"] = self._current.get("title", "") + data
                if self._in_snippet:
                    self._current["snippet"] = self._current.get("snippet", "") + data

        parser = DDGParser()
        parser.feed(resp.text)
        return results[:num]

    async def _search_ddg_api(self, query: str) -> list[dict[str, str]]:
        """Fallback: DuckDuckGo Instant Answers API."""
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_redirect": "1"},
            )
            resp.raise_for_status()
            data = resp.json()

        results = []
        if data.get("AbstractText"):
            results.append(
                {
                    "title": data.get("Heading", query),
                    "url": data.get("AbstractURL", ""),
                    "snippet": data.get("AbstractText", ""),
                }
            )
        for topic in data.get("RelatedTopics", [])[:5]:
            if isinstance(topic, dict) and "Text" in topic:
                results.append(
                    {
                        "title": topic.get("Text", "")[:100],
                        "url": topic.get("FirstURL", ""),
                        "snippet": topic.get("Text", ""),
                    }
                )
        return results
