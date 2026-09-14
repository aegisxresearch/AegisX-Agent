"""MCP (Model Context Protocol) client integration.

Consume tools from any MCP server as first-class, permission-gated AegisX
tools. Servers are external processes launched over stdio; a crash or
malicious server can therefore never reach inside the agent — its tools pass
through the same manifest validation and permission gate as every other tool.
"""

from __future__ import annotations

from aegisx_agent.mcp.client import MCPServerInfo, MCPToolClient, MCPToolSpec
from aegisx_agent.mcp.manager import (
    MCP_CONFIG_FILE,
    MCP_CONNECTIONS_KEY,
    MCP_SERVERS_KEY,
    MCPManager,
    MCPManagerError,
    load_mcp_config,
    save_mcp_config,
)
from aegisx_agent.mcp.tools import MCPBridgeError, MCPToolBridge, sanitize_tool_name

__all__ = [
    "MCP_CONNECTIONS_KEY",
    "MCP_CONFIG_FILE",
    "MCP_SERVERS_KEY",
    "MCPBridgeError",
    "MCPManager",
    "MCPManagerError",
    "MCPServerInfo",
    "MCPToolBridge",
    "MCPToolClient",
    "MCPToolSpec",
    "load_mcp_config",
    "sanitize_tool_name",
    "save_mcp_config",
]
