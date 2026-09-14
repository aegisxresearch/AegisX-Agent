"""Tiny MCP server used by the MCP test-suite.

Real MCP protocol over real stdio — deliberately plain so the tests exercise
the client against an actual server process instead of a mock:

- ``echo``: returns its ``text`` argument (plus metadata in the client).
- ``explode``: always fails with ``isError=True`` (server-side error path).
- ``shouty_echo``: no description, no schema ``type`` (sloppy-server path).

Run directly to use it by hand:  python tests/mcp_demo_server.py
"""

from __future__ import annotations

import mcp.server  # noqa: F401  (ensures a helpful ImportError mentions mcp)
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("aegisx-mcp-demo", version="1.4.2")


@mcp.tool()
def echo(text: str) -> str:
    """Echo the text back unchanged."""
    return text


@mcp.tool()
def explode() -> str:
    """Deliberate failure: the server reports this as an MCP tool error."""
    raise RuntimeError("This is a deliberate failure from the demo server.")


@mcp.tool()
def shouty_echo(text: str):  # type: ignore[no-untyped-def]  (sloppy on purpose)
    """ """
    return text.upper()


if __name__ == "__main__":
    mcp.run()
