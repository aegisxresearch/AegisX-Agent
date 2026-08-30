"""API Caller tool — make HTTP requests to any REST/GraphQL endpoint."""

from __future__ import annotations

import json
from typing import Any

import httpx

from utopia_agent.tools.base import Tool, ToolResult, ToolStatus


class APICallerTool(Tool):
    """Make HTTP requests to any API endpoint."""

    def __init__(self) -> None:
        super().__init__(
            name="api_call",
            description=(
                "Make HTTP requests to any REST API endpoint. "
                "Supports GET, POST, PUT, PATCH, DELETE methods. "
                "Can send JSON body, custom headers, and query parameters. "
                "Use this to interact with external APIs, web services, "
                "or any HTTP endpoint."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The API endpoint URL",
                    },
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                        "description": "HTTP method (default: GET)",
                        "default": "GET",
                    },
                    "headers": {
                        "type": "object",
                        "description": "Custom HTTP headers as key-value pairs",
                        "additionalProperties": {"type": "string"},
                    },
                    "body": {
                        "description": "Request body (JSON object or string)",
                    },
                    "params": {
                        "type": "object",
                        "description": "URL query parameters",
                        "additionalProperties": {"type": "string"},
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Request timeout in seconds (default: 30)",
                        "default": 30,
                    },
                },
                "required": ["url"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        url = kwargs.get("url", "")
        method = kwargs.get("method", "GET").upper()
        headers = kwargs.get("headers", {})
        body = kwargs.get("body")
        params = kwargs.get("params", {})
        timeout = kwargs.get("timeout", 30)

        if not url:
            return ToolResult(status=ToolStatus.ERROR, output="", error="URL is required")

        # Default headers
        if "Content-Type" not in headers and body:
            headers["Content-Type"] = "application/json"
        if "Accept" not in headers:
            headers["Accept"] = "application/json"
        if "User-Agent" not in headers:
            headers["User-Agent"] = "UtopiaAgent/1.0"

        try:
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=True
            ) as client:
                # Prepare body
                json_body = None
                content = None
                if body:
                    if isinstance(body, str):
                        try:
                            json_body = json.loads(body)
                        except json.JSONDecodeError:
                            content = body.encode()
                    else:
                        json_body = body

                resp = await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    content=content,
                )

                # Format response
                status_line = f"HTTP {resp.status_code} {resp.reason_phrase}"
                response_headers = dict(resp.headers)

                # Try to parse response as JSON
                try:
                    response_data = resp.json()
                    response_body = json.dumps(response_data, indent=2, ensure_ascii=False)
                except (json.JSONDecodeError, ValueError):
                    response_body = resp.text[:10_000]

                output = (
                    f"=== {method} {url} ===\n"
                    f"{status_line}\n\n"
                    f"Response Headers:\n"
                )
                for k, v in list(response_headers.items())[:10]:
                    output += f"  {k}: {v}\n"

                output += f"\nResponse Body:\n{response_body}"

                # Add metadata
                metadata = {
                    "status_code": resp.status_code,
                    "method": method,
                    "url": str(resp.url),
                    "content_type": response_headers.get("content-type", ""),
                    "response_size": len(resp.content),
                }

                status = ToolStatus.SUCCESS if resp.status_code < 400 else ToolStatus.ERROR
                error = None if resp.status_code < 400 else f"HTTP {resp.status_code}"

                return ToolResult(
                    status=status,
                    output=output[:20_000],
                    error=error,
                    metadata=metadata,
                )

        except httpx.TimeoutException:
            return ToolResult(
                status=ToolStatus.TIMEOUT,
                output="",
                error=f"Request timed out after {timeout}s",
            )
        except httpx.RequestError as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                output="",
                error=f"Request failed: {type(e).__name__}: {e}",
            )
        except Exception as e:
            return ToolResult(
                status=ToolStatus.ERROR, output="", error=f"Error: {type(e).__name__}: {e}"
            )
