"""Convenience loader for every built-in plugin.

Loaded explicitly:

    agent.load_plugin_module("aegisx_agent.plugins.builtin.all")

Nothing here runs at import time; ``PLUGINS`` is just a tuple of definitions
that the standard plugin registry validates when it is actually loaded.
"""

from __future__ import annotations

from aegisx_agent.plugins.builtin.browser import PLUGINS as BROWSER_PLUGINS
from aegisx_agent.plugins.builtin.database import PLUGINS as DATABASE_PLUGINS
from aegisx_agent.plugins.builtin.github import PLUGINS as GITHUB_PLUGINS

PLUGINS = (*BROWSER_PLUGINS, *DATABASE_PLUGINS, *GITHUB_PLUGINS)
