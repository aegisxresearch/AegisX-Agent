"""MCP tool bridge: expose server tools as permission-gated AegisX tools.

An MCP tool is by definition external code with unknown side effects, so it
can never claim to be ``SAFE`` — a read-only-looking tool still talks to a
process the agent does not control. The default risk is therefore
``CAUTION`` (external process / network), and hints like ``dangerous`` or
``destructive`` in the server's own tool description escalate it. The global
permission gate remains authoritative for every call, exactly as for native
plugins.
"""

from __future__ import annotations

import re
from typing import Any

from aegisx_agent.mcp.client import MCPToolClient, MCPToolSpec
from aegisx_agent.plugins.manifest import (
    PluginDefinition,
    PluginManifest,
    PluginPermissionPolicy,
)
from aegisx_agent.tools.base import ToolRisk

_DEFAULT_RISK = ToolRisk.CAUTION
_DANGEROUS_HINTS = re.compile(r"\b(dangerous|destructive|irreversible)\b", re.IGNORECASE)
_TOOL_NAME_SAFE = re.compile(r"[^a-z0-9_-]+")
_SEMVERISH = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")

#: Config keys an MCP server entry understands (documented in the MCP guide).
SUPPORTED_SERVER_CONFIG_KEYS = frozenset(
    {"command", "args", "env", "risk", "tool_risks", "requires_approval", "version"}
)


class MCPBridgeError(ValueError):
    """Raised when a server's tool catalog cannot be bridged safely."""


def sanitize_tool_name(remote_name: str) -> str:
    """Normalize a remote tool name to the plugin tool-name rules.

    Lowercase, collapse disallowed characters to ``_``, guarantee a leading
    letter, and cap at 64 characters. Collisions after sanitization are the
    caller's problem (the bridge raises on duplicates).
    """
    cleaned = _TOOL_NAME_SAFE.sub("_", remote_name.strip().lower())
    cleaned = cleaned.strip("_-") or "tool"
    if not cleaned[0].isalpha():
        cleaned = f"tool_{cleaned}"
    return cleaned[:64]


def _server_risk(server_config: dict[str, Any]) -> ToolRisk:
    """Default risk for every tool of a server, from its config."""
    declared = server_config.get("risk")
    if declared is None:
        return _DEFAULT_RISK
    try:
        return ToolRisk(str(declared))
    except ValueError as error:
        raise MCPBridgeError(
            f"Invalid risk {declared!r} in MCP server config; "
            "expected one of: safe, caution, dangerous"
        ) from error


def _tool_risk(
    spec: MCPToolSpec, server_config: dict[str, Any], default: ToolRisk
) -> ToolRisk:
    """Resolve one tool's risk: per-tool override, then description hints."""
    overrides = server_config.get("tool_risks") or {}
    if isinstance(overrides, dict) and spec.name in overrides:
        try:
            return ToolRisk(str(overrides[spec.name]))
        except ValueError as error:
            raise MCPBridgeError(
                f"Invalid tool_risks entry for '{spec.name}': {overrides[spec.name]!r}"
            ) from error
    if _DANGEROUS_HINTS.search(spec.description):
        return ToolRisk.DANGEROUS
    return default


def _server_version(server_config: dict[str, Any], reported: str) -> str:
    """Pick a manifest version: config override, else a semver-safe fallback."""
    for candidate in (server_config.get("version"), reported):
        text = str(candidate or "").strip()
        if _SEMVERISH.fullmatch(text):
            return text
    return "0.0.0"


class MCPToolBridge:
    """Build plugin definitions from a server's advertised tool catalog."""

    def build_definitions(
        self,
        server_id: str,
        server_config: dict[str, Any],
        specs: list[MCPToolSpec],
        client: MCPToolClient,
    ) -> list[PluginDefinition]:
        """Wrap every spec as a validated, gated plugin definition.

        All definitions share the ``mcp_<server_id>`` plugin id, so unloading
        that id removes the whole server's toolset at once.
        """
        plugin_id = f"mcp_{sanitize_tool_name(server_id)[:60]}"
        version = _server_version(server_config, client.server_version)
        default_risk = _server_risk(server_config)
        policy = PluginPermissionPolicy(
            requires_approval=bool(server_config.get("requires_approval", False)),
            allow_in_read_only=False,
        )

        definitions: list[PluginDefinition] = []
        seen_names: set[str] = set()
        for spec in specs:
            tool_name = sanitize_tool_name(spec.name)
            if tool_name in seen_names:
                raise MCPBridgeError(
                    f"MCP server '{server_id}' advertises duplicate tools that "
                    f"collapse to the same name: '{tool_name}'"
                )
            seen_names.add(tool_name)
            parameters = spec.parameters if isinstance(spec.parameters, dict) else {}
            manifest = PluginManifest(
                plugin_id=plugin_id,
                version=version,
                tool_name=tool_name,
                description=spec.description or f"Tool '{spec.name}' from MCP server '{server_id}'",
                parameters=parameters,
                risk=_tool_risk(spec, server_config, default_risk),
                permission=policy,
            )
            definitions.append(
                PluginDefinition(manifest=manifest, handler=self._handler(spec, client))
            )
        if not definitions:
            raise MCPBridgeError(f"MCP server '{server_id}' advertises no tools")
        return definitions

    @staticmethod
    def _handler(
        spec: MCPToolSpec, client: MCPToolClient
    ) -> Any:
        """Async handler that forwards one call over the MCP session."""

        async def handler(**kwargs: Any) -> str:
            result = await client.call_tool(spec.name, dict(kwargs))
            if not result.is_success:
                # Raise so FunctionTool surfaces a proper ERROR ToolResult to
                # the LLM instead of masking a server-side failure as output.
                raise MCPBridgeError(result.error or "MCP tool call failed")
            return result.output

        handler.__name__ = f"mcp_{spec.server_id}_{spec.name}"
        handler.__doc__ = spec.description
        return handler


__all__ = [
    "MCPBridgeError",
    "MCPToolBridge",
    "SUPPORTED_SERVER_CONFIG_KEYS",
    "sanitize_tool_name",
]
