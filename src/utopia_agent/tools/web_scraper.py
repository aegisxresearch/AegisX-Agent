"""Web Scraper tool — fetch and parse web pages."""

from __future__ import annotations

import re
from typing import Any

import httpx

from utopia_agent.tools.base import Tool, ToolResult, ToolStatus


class WebScraperTool(Tool):
    """Fetch and extract content from web pages."""

    def __init__(self) -> None:
        super().__init__(
            name="web_scrape",
            description=(
                "Fetch a web page and extract its content. "
                "Can extract text, links, images, meta tags, or specific elements. "
                "Use this to scrape websites, read articles, extract data from pages, "
                "or get structured information from URLs."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to scrape",
                    },
                    "extract": {
                        "type": "string",
                        "enum": ["text", "links", "images", "meta", "html", "all"],
                        "description": "What to extract (default: text)",
                        "default": "text",
                    },
                    "selector": {
                        "type": "string",
                        "description": "CSS selector to target specific elements (optional)",
                    },
                    "max_length": {
                        "type": "integer",
                        "description": "Max content length in characters (default: 10000)",
                        "default": 10000,
                    },
                },
                "required": ["url"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        url = kwargs.get("url", "")
        extract = kwargs.get("extract", "text")
        selector = kwargs.get("selector")
        max_length = kwargs.get("max_length", 10000)

        if not url:
            return ToolResult(status=ToolStatus.ERROR, output="", error="URL is required")

        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        try:
            async with httpx.AsyncClient(
                timeout=30, follow_redirects=True
            ) as client:
                resp = await client.get(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; UtopiaAgent/1.0)",
                        "Accept": "text/html,application/xhtml+xml,*/*",
                    },
                )
                resp.raise_for_status()

            html = resp.text

            if extract == "links":
                return self._extract_links(html, url, max_length)
            elif extract == "images":
                return self._extract_images(html, url, max_length)
            elif extract == "meta":
                return self._extract_meta(html, url)
            elif extract == "html":
                content = html[:max_length]
                return ToolResult(status=ToolStatus.SUCCESS, output=content)
            elif extract == "all":
                return self._extract_all(html, url, max_length)
            else:
                return self._extract_text(html, url, max_length, selector)

        except httpx.TimeoutException:
            return ToolResult(
                status=ToolStatus.TIMEOUT, output="", error="Request timed out"
            )
        except httpx.HTTPStatusError as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                output="",
                error=f"HTTP {e.response.status_code}: {e.response.reason_phrase}",
            )
        except Exception as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                output="",
                error=f"Scraping failed: {type(e).__name__}: {e}",
            )

    def _extract_text(
        self, html: str, url: str, max_length: int, selector: str | None = None
    ) -> ToolResult:
        """Extract readable text from HTML."""
        # Remove scripts, styles, nav, footer, header
        text = html
        for tag in ["script", "style", "nav", "footer", "header", "aside", "noscript"]:
            text = re.sub(f"<{tag}[^>]*>.*?</{tag}>", "", text, flags=re.DOTALL | re.IGNORECASE)

        # Remove HTML tags
        text = re.sub(r"<[^>]+>", " ", text)

        # Clean whitespace
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r" +", " ", text)

        # Decode entities
        text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")

        if len(text) > max_length:
            text = text[:max_length] + "\n\n... [truncated]"

        output = f"=== Content from {url} ===\n\n{text}"
        return ToolResult(
            status=ToolStatus.SUCCESS,
            output=output,
            metadata={"url": url, "content_length": len(text)},
        )

    def _extract_links(self, html: str, url: str, max_length: int) -> ToolResult:
        """Extract all links from HTML."""
        from urllib.parse import urljoin

        links = re.findall(r'href=["\']([^"\']+)["\']', html, re.IGNORECASE)
        unique_links = []
        seen = set()

        for link in links:
            full_url = urljoin(url, link)
            if full_url not in seen and not full_url.startswith(("javascript:", "mailto:")):
                seen.add(full_url)
                unique_links.append(full_url)

        output = f"=== Links from {url} ===\n\n"
        for i, link in enumerate(unique_links[:100], 1):
            output += f"{i}. {link}\n"

        if len(unique_links) > 100:
            output += f"\n... and {len(unique_links) - 100} more links"

        output += f"\n\nTotal: {len(unique_links)} unique links"
        return ToolResult(status=ToolStatus.SUCCESS, output=output[:max_length])

    def _extract_images(self, html: str, url: str, max_length: int) -> ToolResult:
        """Extract all images from HTML."""
        from urllib.parse import urljoin

        images = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE)
        unique_images = []
        seen = set()

        for img in images:
            full_url = urljoin(url, img)
            if full_url not in seen:
                seen.add(full_url)
                unique_images.append(full_url)

        output = f"=== Images from {url} ===\n\n"
        for i, img in enumerate(unique_images[:50], 1):
            output += f"{i}. {img}\n"

        output += f"\nTotal: {len(unique_images)} images"
        return ToolResult(status=ToolStatus.SUCCESS, output=output[:max_length])

    def _extract_meta(self, html: str, url: str) -> ToolResult:
        """Extract meta tags from HTML."""
        metas = {}

        # Title
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
        if title_match:
            metas["title"] = title_match.group(1).strip()

        # Meta tags
        for match in re.finditer(
            r'<meta[^>]+(?:name|property)=["\']([^"\']+)["\'][^>]+content=["\']([^"\']+)["\']',
            html,
            re.IGNORECASE,
        ):
            metas[match.group(1)] = match.group(2)

        # Reverse: content before name
        for match in re.finditer(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:name|property)=["\']([^"\']+)["\']',
            html,
            re.IGNORECASE,
        ):
            metas[match.group(2)] = match.group(1)

        output = f"=== Meta Information for {url} ===\n\n"
        for key, value in metas.items():
            output += f"{key}: {value}\n"

        if not metas:
            output += "No meta tags found."

        return ToolResult(status=ToolStatus.SUCCESS, output=output)

    def _extract_all(self, html: str, url: str, max_length: int) -> ToolResult:
        """Extract everything."""
        from urllib.parse import urljoin

        parts = [f"=== Full Extraction from {url} ===\n"]

        # Meta
        meta_result = self._extract_meta(html, url)
        parts.append(meta_result.output)

        # Text
        text_result = self._extract_text(html, url, max_length // 2)
        parts.append(f"\n--- Text Content ---\n{text_result.output}")

        # Links
        links_result = self._extract_links(html, url, max_length // 4)
        parts.append(f"\n--- Links ---\n{links_result.output}")

        output = "\n".join(parts)[:max_length]
        return ToolResult(status=ToolStatus.SUCCESS, output=output)
