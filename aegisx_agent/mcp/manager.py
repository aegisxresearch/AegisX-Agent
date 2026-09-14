"""MCP server manager: config persistence, connections, and bridging.

Servers are declared in ``<data_dir>/mcp_servers.json`` (the same shape as
Claude Desktop's config), loaded explicitly — like every other plugin — and
bridged into the tool registry as permission-gated plugin tools.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aegisx_agent.mcp.client import MCPClientError, MCPToolClient
from aegisx_agent.mcp.tools import (
    SUPPORTED_SERVER_CONFIG_KEYS,
    MCPBridgeError,
    MCPToolBridge,
)
from aegisx_agent.plugins.manifest import PluginDefinition, PluginError
from aegisx_agent.plugins.registry import PluginRegistry
from aegisx_agent.tools.base import ToolRisk

MCP_CONFIG_FILE = "mcp_servers.json"
MCP_SERVERS_KEY = "mcpServers"
MCP_CONNECTIONS_KEY = "mcpConnections"


class MCPManagerError(ValueError):
    """Raised when MCP server configuration or lifecycle requests are invalid."""


def load_mcp_config(path: Path) -> dict[str, dict[str, Any]]:
    """Read server configs from ``mcp_servers.json`` (empty dict if absent)."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise MCPManagerError(f"MCP config is not valid JSON ({path}): {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get(MCP_SERVERS_KEY), dict):
        raise MCPManagerError(
            f"MCP config must contain a '{MCP_SERVERS_KEY}' object: {path}"
        )
    servers: dict[str, dict[str, Any]] = data[MCP_SERVERS_KEY]
    for server_id, server_config in servers.items():
        if not isinstance(server_config, dict):
            raise MCPManagerError(f"MCP server '{server_id}' config must be an object")
    return servers


def save_mcp_config(path: Path, servers: dict[str, dict[str, Any]]) -> None:
    """Atomically persist server configs to ``mcp_servers.json``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {MCP_SERVERS_KEY: servers}
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class MCPManager:
    """Owns MCP client connections and the bridged definitions per server.

    Bridged tools are registered through the plugin registry, so they show up
    in ``/plugin list`` with gate verdicts and can be unloaded by their
    ``mcp_<server>`` plugin id like any other plugin.
    """

    def __init__(
        self, data_path: Path, plugin_registry: PluginRegistry, tool_registry: Any
    ) -> None:
        self._config_path = data_path / MCP_CONFIG_FILE
        self._plugin_registry = plugin_registry
        self._tool_registry = tool_registry
        self._clients: dict[str, MCPToolClient] = {}
        self._definitions: dict[str, list[PluginDefinition]] = {}
        self._bridge = MCPToolBridge()

    # ------------------------------------------------------------------ #
    # Config
    # ------------------------------------------------------------------ #

    @property
    def config_path(self) -> Path:
        return self._config_path

    def load_config(self) -> list[str]:
        """Load and validate the persisted server config; return server ids."""
        servers = load_mcp_config(self._config_path)
        self.validate_server_configs(servers)
        return list(servers)

    def persist_server(
        self, server_id: str, server_config: dict[str, Any]
    ) -> None:
        """Merge one server into the config file and save it atomically."""
        servers = load_mcp_config(self._config_path)
        servers[server_id] = server_config
        self.validate_server_configs(servers)
        save_mcp_config(self._config_path, servers)

    def remove_server_config(self, server_id: str) -> bool:
        """Drop one server from the config file; ``False`` if it was absent."""
        servers = load_mcp_config(self._config_path)
        if server_id not in servers:
            return False
        del servers[server_id]
        save_mcp_config(self._config_path, servers)
        return True

    @staticmethod
    def validate_server_configs(servers: dict[str, dict[str, Any]]) -> None:
        """Reject unknown keys and invalid risk values early — typos fail loudly."""
        for server_id, server_config in servers.items():
            unknown = set(server_config) - SUPPORTED_SERVER_CONFIG_KEYS
            if unknown:
                raise MCPManagerError(
                    f"MCP server '{server_id}' has unknown config keys: "
                    f"{sorted(unknown)}; supported keys: {sorted(SUPPORTED_SERVER_CONFIG_KEYS)}"
                )
            if not str(server_config.get("command", "")).strip():
                raise MCPManagerError(f"MCP server '{server_id}' has no 'command'")
            risk = server_config.get("risk")
            if risk is not None:
                try:
                    ToolRisk(str(risk))
                except ValueError as error:
                    raise MCPManagerError(
                        f"MCP server '{server_id}' has invalid risk {risk!r}; "
                        "expected one of: safe, caution, dangerous"
                    ) from error
            tool_risks = server_config.get("tool_risks") or {}
            if isinstance(tool_risks, dict):
                for tool_name, tool_risk in tool_risks.items():
                    try:
                        ToolRisk(str(tool_risk))
                    except ValueError as error:
                        raise MCPManagerError(
                            f"MCP server '{server_id}' has invalid tool_risks entry "
                            f"for '{tool_name}': {tool_risk!r}"
                        ) from error

    # ------------------------------------------------------------------ #
    # Connections
    # ------------------------------------------------------------------ #

    def get_server_config(self, server_id: str) -> dict[str, Any]:
        """One server's persisted config, raising if it is not configured."""
        servers = load_mcp_config(self._config_path)
        if server_id not in servers:
            raise MCPManagerError(
                f"MCP server '{server_id}' is not configured in {self._config_path}"
            )
        return servers[server_id]

    def get_client(self, server_id: str) -> MCPToolClient | None:
        """The live client for a server, if it is connected."""
        return self._clients.get(server_id)

    def connected_servers(self) -> list[str]:
        """Ids of servers with a live session."""
        return sorted(self._clients)

    def is_connected(self, server_id: str) -> bool:
        return server_id in self._clients

    # ------------------------------------------------------------------ #
    # Bridging
    # ------------------------------------------------------------------ #

    def bridged_plugin_ids(self) -> list[str]:
        """Plugin ids currently bridged into the registry."""
        return sorted(self._definitions)

    def definitions_for(self, server_id: str) -> list[PluginDefinition]:
        """The definitions bridged for one server (empty if not connected)."""
        return list(self._definitions.get(f"mcp_{server_id}", []))

    async def connect_server(self, server_id: str, server_config: dict[str, Any]) -> list[str]:
        """Connect, discover tools, bridge them, and register them gated.

        Returns the qualified tool names. Any failure after the handshake
        tears the connection back down, so a half-connected server is never
        left behind.
        """
        self.validate_server_configs({server_id: server_config})
        if server_id in self._clients:
            raise MCPManagerError(f"MCP server '{server_id}' is already connected")
        client = MCPToolClient(server_id, server_config)
        try:
            specs = await client.list_tools()
            definitions = self._bridge.build_definitions(
                server_id=server_id,
                server_config=server_config,
                specs=specs,
                client=client,
            )
            for definition in definitions:
                plugin_tool = self._plugin_registry.register(definition)
                self._tool_registry.register(plugin_tool)
            # Everything succeeded — only now does the server become "live".
            self._clients[server_id] = client
            self._definitions[f"mcp_{server_id}"] = definitions
        except (MCPClientError, MCPBridgeError, PluginError, OSError) as error:
            await client.close()
            raise MCPManagerError(
                f"Failed to connect MCP server '{server_id}': {error}"
            ) from error
        return [
            definition.manifest.qualified_tool_name for definition in definitions
        ]

    async def disconnect_server(self, server_id: str) -> bool:
        """Unregister a server's tools and close its session.

        Returns ``False`` for a server that is not currently connected, so
        the CLI can report "not connected" instead of raising.
        """
        client = self._clients.pop(server_id, None)
        if client is None:
            return False
        plugin_id = f"mcp_{server_id}"
        # Unload via the plugin registry when the definitions are still there;
        # ``/plugin unload`` may already have removed them, in which case the
        # fallback dict still knows the tool names to unregister.
        definitions = self._plugin_registry.unregister(plugin_id)
        if definitions is None:
            definitions = self._definitions.pop(plugin_id, [])
        else:
            self._definitions.pop(plugin_id, None)
        for definition in definitions:
            self._tool_registry.unregister(definition.manifest.qualified_tool_name)
        await client.close()
        return True

    async def close_all(self) -> None:
        """Tear every connection down (used at agent shutdown)."""
        for server_id in list(self._clients):
            await self.disconnect_server(server_id)


__all__ = [
    "MCP_CONNECTIONS_KEY",
    "MCP_CONFIG_FILE",
    "MCP_SERVERS_KEY",
    "MCPManager",
    "MCPManagerError",
    "load_mcp_config",
    "save_mcp_config",
]
