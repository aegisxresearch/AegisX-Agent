"""Versioned tool plugins for explicit AegisX extension."""

from aegisx_agent.plugins.manifest import (
    PLUGIN_API_VERSION,
    PluginDefinition,
    PluginError,
    PluginManifest,
    PluginPermissionPolicy,
    define_plugin,
)
from aegisx_agent.plugins.registry import PluginRegistry, PluginTool

__all__ = [
    "PLUGIN_API_VERSION",
    "PluginDefinition",
    "PluginError",
    "PluginManifest",
    "PluginPermissionPolicy",
    "PluginRegistry",
    "PluginTool",
    "define_plugin",
]
