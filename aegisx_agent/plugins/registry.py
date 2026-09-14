"""Dynamic plugin loading for versioned AegisX tool plugins."""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from aegisx_agent.plugins.manifest import (
    PLUGIN_API_VERSION,
    PluginDefinition,
    PluginError,
    PluginManifest,
    PluginPermissionPolicy,
)
from aegisx_agent.tools.base import FunctionTool, ToolRisk


class PluginTool(FunctionTool):
    """Function tool whose risk is constrained by its plugin policy."""

    def __init__(self, definition: PluginDefinition) -> None:
        self.definition = definition
        manifest = definition.manifest
        super().__init__(
            name=manifest.qualified_tool_name,
            description=manifest.description,
            func=definition.handler,
            parameters=manifest.parameters,
            risk=manifest.risk,
        )

    def risk_for(self, arguments: dict[str, Any]) -> ToolRisk:
        """Apply plugin policy before the global permission gate evaluates it."""
        return self.definition.manifest.permission.effective_risk(self.risk)


class PluginRegistry:
    """Load and validate plugins, then register them in a tool registry.

    A plugin may expose **several tools** under one ``plugin_id`` (a GitHub
    plugin with ``repo_info`` and ``list_issues``, say). All definitions of a
    plugin share its version lock: they load together, and unloading the
    ``plugin_id`` removes every tool it brought.
    """

    def __init__(self) -> None:
        self._definitions: dict[str, list[PluginDefinition]] = {}

    def register(self, definition: PluginDefinition) -> PluginTool:
        """Validate and register one definition, grouped under its plugin_id."""
        definition.validate()
        plugin_id = definition.manifest.plugin_id
        version = definition.manifest.version
        existing = self._definitions.get(plugin_id)
        if existing and existing[0].manifest.version != version:
            raise PluginError(
                f"Plugin '{plugin_id}' is already loaded at version "
                f"{existing[0].manifest.version}; unload it before changing versions."
            )
        if not any(
            item.manifest.qualified_tool_name == definition.manifest.qualified_tool_name
            for item in (existing or [])
        ):
            self._definitions.setdefault(plugin_id, []).append(definition)
        return PluginTool(definition)

    def unregister(self, plugin_id: str) -> list[PluginDefinition] | None:
        """Remove and return every definition of a plugin."""
        return self._definitions.pop(plugin_id, None)

    def get(self, plugin_id: str) -> list[PluginDefinition] | None:
        """Get every loaded definition of a plugin by ID."""
        return self._definitions.get(plugin_id)

    def list_plugins(self) -> list[PluginManifest]:
        """List loaded manifests in registration order (one per tool)."""
        return [
            definition.manifest
            for definitions in self._definitions.values()
            for definition in definitions
        ]

    def load_module(self, module: str | ModuleType) -> list[PluginDefinition]:
        """Load ``PluginDefinition`` values exported by a module.

        Loading is explicit; importing AegisX never executes arbitrary plugin
        modules. A module can expose one definition as ``PLUGIN`` or many in
        ``PLUGINS``.
        """
        loaded = importlib.import_module(module) if isinstance(module, str) else module
        return self._definitions_from_module(loaded)

    def load_path(self, path: str | Path) -> list[PluginDefinition]:
        """Load plugin definitions from a Python file by explicit path."""
        plugin_path = Path(path).expanduser().resolve()
        if not plugin_path.is_file() or plugin_path.suffix != ".py":
            raise PluginError(f"Plugin path is not a Python file: {path}")
        module_name = f"aegisx_external_plugin_{plugin_path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, plugin_path)
        if spec is None or spec.loader is None:
            raise PluginError(f"Unable to load plugin module: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return self._definitions_from_module(module)

    def install_into(self, registry: Any) -> list[PluginTool]:
        """Register every loaded definition as a tool in an existing registry."""
        tools = []
        for definitions in self._definitions.values():
            for definition in definitions:
                tool = PluginTool(definition)
                registry.register(tool)
                tools.append(tool)
        return tools

    @staticmethod
    def _definitions_from_module(module: ModuleType) -> list[PluginDefinition]:
        """Extract and validate the explicit plugin exports from a module."""
        candidates: list[PluginDefinition] = []
        single = getattr(module, "PLUGIN", None)
        many = getattr(module, "PLUGINS", ())
        if single is not None:
            candidates.append(single)
        candidates.extend(many)
        if not candidates or not all(isinstance(item, PluginDefinition) for item in candidates):
            raise PluginError(
                "Plugin module must export PLUGIN or PLUGINS containing PluginDefinition values"
            )
        return candidates


__all__ = [
    "PLUGIN_API_VERSION",
    "PluginDefinition",
    "PluginError",
    "PluginManifest",
    "PluginPermissionPolicy",
    "PluginRegistry",
    "PluginTool",
]
