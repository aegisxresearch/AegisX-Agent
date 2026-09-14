"""Versioned tool-plugin contracts and permission metadata."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from aegisx_agent.tools.base import ToolRisk

PLUGIN_API_VERSION = "1"
_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$")
_PLUGIN_ID = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")


class PluginError(ValueError):
    """Raised when a plugin manifest or definition is invalid."""


@dataclass(frozen=True)
class PluginPermissionPolicy:
    """Permission requirements declared by a plugin.

    The policy can only make a plugin stricter. The global ``PermissionGate``
    remains authoritative and every call still passes through it.
    """

    requires_approval: bool = False
    allow_in_read_only: bool = True

    def effective_risk(self, declared_risk: ToolRisk) -> ToolRisk:
        """Return the risk sent to the global permission gate."""
        if self.requires_approval:
            return ToolRisk.DANGEROUS
        if not self.allow_in_read_only and declared_risk is ToolRisk.SAFE:
            return ToolRisk.CAUTION
        return declared_risk


@dataclass(frozen=True)
class PluginManifest:
    """Metadata needed to expose one plugin as one callable tool."""

    plugin_id: str
    version: str
    tool_name: str
    description: str
    parameters: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    risk: ToolRisk = ToolRisk.SAFE
    permission: PluginPermissionPolicy = field(default_factory=PluginPermissionPolicy)
    api_version: str = PLUGIN_API_VERSION

    @property
    def qualified_tool_name(self) -> str:
        """Stable tool name that prevents collisions between plugins."""
        return f"plugin_{self.plugin_id}_{self.tool_name}"

    def validate(self) -> None:
        """Validate compatibility and the tool schema before loading."""
        if not _PLUGIN_ID.fullmatch(self.plugin_id):
            raise PluginError(
                "plugin_id must be 2-64 characters, start with a lowercase letter, "
                "and contain only lowercase letters, digits, '_' or '-'."
            )
        if not _SEMVER.fullmatch(self.version):
            raise PluginError(f"Plugin '{self.plugin_id}' version must be semver: {self.version!r}")
        if self.api_version != PLUGIN_API_VERSION:
            raise PluginError(
                f"Plugin '{self.plugin_id}' requires unsupported plugin API "
                f"{self.api_version!r}; supported API is {PLUGIN_API_VERSION!r}."
            )
        if not _TOOL_NAME.fullmatch(self.tool_name):
            raise PluginError("tool_name must be a lowercase identifier up to 64 characters")
        if not self.description.strip():
            raise PluginError("description must not be empty")
        if not isinstance(self.parameters, dict) or self.parameters.get("type") != "object":
            raise PluginError("plugin parameters schema must have type='object'")
        properties = self.parameters.get("properties", {})
        if not isinstance(properties, dict):
            raise PluginError("plugin parameters.properties must be an object")


@dataclass(frozen=True)
class PluginDefinition:
    """A manifest paired with the sync or async function that implements it."""

    manifest: PluginManifest
    handler: Callable[..., Any]

    def validate(self) -> None:
        """Validate both metadata and executable implementation."""
        self.manifest.validate()
        if not callable(self.handler):
            raise PluginError(f"Plugin '{self.manifest.plugin_id}' handler is not callable")


def define_plugin(
    manifest: PluginManifest,
) -> Callable[[Callable[..., Any]], PluginDefinition]:
    """Decorator that turns a function into a validated plugin definition."""

    def decorator(handler: Callable[..., Any]) -> PluginDefinition:
        definition = PluginDefinition(manifest=manifest, handler=handler)
        definition.validate()
        return definition

    return decorator
