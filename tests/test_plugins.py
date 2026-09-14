"""Versioned plugin loading, schema exposure, and permission enforcement."""

from __future__ import annotations

import sys
from types import ModuleType

import pytest
from support import run

from aegisx_agent.plugins import (
    PluginError,
    PluginManifest,
    PluginPermissionPolicy,
    PluginRegistry,
    define_plugin,
)
from aegisx_agent.security.permissions import PermissionGate, PermissionMode
from aegisx_agent.tools.registry import ToolRegistry


def _definition(plugin_id: str = "weather", version: str = "1.0.0"):
    manifest = PluginManifest(
        plugin_id=plugin_id,
        version=version,
        tool_name="lookup",
        description="Look up a weather value.",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    )

    @define_plugin(manifest)
    def lookup(city: str) -> str:
        return f"weather:{city}"

    return lookup


def test_plugin_registers_with_versioned_schema_and_executes_through_registry() -> None:
    plugins = PluginRegistry()
    tool = plugins.register(_definition())
    tools = ToolRegistry()
    plugins.install_into(tools)

    assert tool.name == "plugin_weather_lookup"
    assert tool.to_schema()["function"]["parameters"]["required"] == ["city"]
    assert run(tools.execute(tool.name, {"city": "Jakarta"})).output == "weather:Jakarta"
    assert plugins.list_plugins()[0].version == "1.0.0"


def test_plugin_policy_can_make_a_safe_plugin_require_approval() -> None:
    manifest = PluginManifest(
        plugin_id="sensitive",
        version="1.0.0",
        tool_name="read",
        description="Read sensitive data.",
        permission=PluginPermissionPolicy(requires_approval=True),
    )

    @define_plugin(manifest)
    def read() -> str:
        return "secret"

    tools = ToolRegistry(gate=PermissionGate(mode=PermissionMode.READ_ONLY))
    tool = PluginRegistry().register(read)
    tools.register(tool)

    result = run(tools.execute(tool.name, {}))

    assert result.is_success is False
    assert result.metadata["denied"] is True
    assert "read-only mode" in result.error


def test_plugin_validation_rejects_unsupported_api_and_version_changes() -> None:
    with pytest.raises(PluginError, match="semver"):
        PluginManifest(
            plugin_id="bad",
            version="v1",
            tool_name="run",
            description="Bad version",
        ).validate()

    registry = PluginRegistry()
    registry.register(_definition(version="1.0.0"))
    with pytest.raises(PluginError, match="already loaded"):
        registry.register(_definition(version="2.0.0"))


def test_plugin_module_loader_accepts_explicit_exports_and_rejects_invalid_modules() -> None:
    module = ModuleType("test_plugin_module")
    module.PLUGIN = _definition("module_plugin")
    registry = PluginRegistry()

    loaded = registry.load_module(module)

    assert loaded[0].manifest.plugin_id == "module_plugin"

    invalid = ModuleType("invalid_plugin_module")
    invalid.PLUGIN = object()
    with pytest.raises(PluginError, match="PluginDefinition"):
        registry.load_module(invalid)


def test_plugin_path_loader_is_explicit_and_supports_a_python_file(tmp_path) -> None:
    path = tmp_path / "external_plugin.py"
    path.write_text(
        "from aegisx_agent.plugins import PluginManifest, define_plugin\n"
        "PLUGIN = define_plugin(PluginManifest("
        "plugin_id='external', version='1.0.0', tool_name='run', "
        "description='Run external operation.'" "))(lambda: 'external-ok')\n"
    )

    loaded = PluginRegistry().load_path(path)

    assert loaded[0].manifest.qualified_tool_name == "plugin_external_run"
    sys.modules.pop("aegisx_external_plugin_external_plugin", None)
