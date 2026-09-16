"""Feature probe for the MCP demo server's SDK requirements.

``pyproject.toml`` pins the optional ``mcp`` extra to ``>=2.0.0`` and
``tests/mcp_demo_server.py`` is written against the 2.x server API. An older
major version still satisfies ``MCP_AVAILABLE`` (the package imports fine),
but every test that spawns the demo server then dies with a misleading
``McpError: Connection closed`` — the subprocess crashes on import before the
stdio transport even exists. These probes turn that failure mode into a clear
skip reason instead of twelve red tests.
"""

from __future__ import annotations

import importlib.util

import pytest

from aegisx_agent.mcp.client import MCP_AVAILABLE

#: Present in mcp 2.x only — the demo server imports it at module level.
_MCP_2_SERVER_API = "mcp.server.mcpserver"


def _module_available(name: str) -> bool:
    """Whether ``name`` is importable (no import side effects)."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


DEMO_SERVER_SUPPORTED = MCP_AVAILABLE and _module_available(_MCP_2_SERVER_API)

DEMO_SERVER_SKIP_REASON = (
    "tests/mcp_demo_server.py needs the mcp 2.x server API "
    f"(no {_MCP_2_SERVER_API}); install with: pip install 'aegisx-agent[mcp]'"
)

#: Mark for tests that launch the demo server subprocess. Tests that only
#: exercise client-side logic (config validation, error paths, the bridge)
#: must NOT use it — they run fine on any installed mcp version.
needs_demo_server = pytest.mark.skipif(
    not DEMO_SERVER_SUPPORTED, reason=DEMO_SERVER_SKIP_REASON
)
