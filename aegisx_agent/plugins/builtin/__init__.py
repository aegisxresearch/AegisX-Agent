"""Built-in tool plugins shipped with AegisX.

Each module here exports ``PLUGINS`` (or ``PLUGIN``) as
:class:`~aegisx_agent.plugins.manifest.PluginDefinition` values, so they load
through exactly the same explicit, versioned, permission-gated path as any
external plugin:

    agent.load_plugin_module("aegisx_agent.plugins.builtin.github")
    agent.load_plugin_module("aegisx_agent.plugins.builtin.all")
"""
