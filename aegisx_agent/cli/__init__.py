"""Rich terminal CLI for AegisX.

Layout:
    aegisx_agent.cli.app          — shared Typer app + console
    aegisx_agent.cli.main         — config plumbing + typer entry points
    aegisx_agent.cli.interactive  — chat loop, animated progress, slash dispatch
    aegisx_agent.cli.commands.*   — slash-command handlers by concern

This package re-exports the symbols other modules (and the test suite)
reach for, so ``from aegisx_agent.cli import main as cli`` keeps working
regardless of which internal module defines a name.
"""

from aegisx_agent.cli.app import app, console
from aegisx_agent.cli.commands.code import (
    _handle_code_command,
    _handle_git_command,
    _handle_test_command,
)
from aegisx_agent.cli.commands.mcp import (
    MCP_USAGE,
    _handle_mcp_command,
    _print_servers_table,
)
from aegisx_agent.cli.commands.observability import (
    _handle_audit_command,
    _handle_usage_command,
    _print_audit_table,
    _print_usage_summary,
    _usage_to_json,
)
from aegisx_agent.cli.commands.permissions import (
    _handle_permissions_command,
    _print_tools_table,
)
from aegisx_agent.cli.commands.plugins import (
    _handle_plugin_command,
    _print_plugins_table,
)
from aegisx_agent.cli.commands.schedule import (
    _flags_to_schedule,
    _handle_schedule_command,
    _print_schedule_logs,
    _print_schedule_results,
    _resolve_schedule,
    _split_flags,
)
from aegisx_agent.cli.interactive import (
    AnimatedProgress,
    _handle_slash_command,
    _show_command_menu,
)

# main must be imported last: it wires the entry points onto ``app`` and
# defines the module-level state (``_agent``, ``CONFIG_FILE``) that tests patch.
from aegisx_agent.cli.main import (  # noqa: E402
    CONFIG_FILE,
    _agent,
    _get_agent,
    _get_config,
    _permission_prompt,
)
from aegisx_agent.cli.main import (
    app as _app_object,
)

__all__ = [
    "AnimatedProgress",
    "CONFIG_FILE",
    "_agent",
    "_app_object",
    "_flags_to_schedule",
    "_get_agent",
    "_get_config",
    "_handle_audit_command",
    "_handle_code_command",
    "_handle_git_command",
    "_handle_mcp_command",
    "_handle_permissions_command",
    "_handle_plugin_command",
    "_handle_schedule_command",
    "_handle_slash_command",
    "_handle_test_command",
    "_handle_usage_command",
    "_permission_prompt",
    "MCP_USAGE",
    "_print_audit_table",
    "_print_plugins_table",
    "_print_servers_table",
    "_print_schedule_logs",
    "_print_schedule_results",
    "_print_tools_table",
    "_print_usage_summary",
    "_resolve_schedule",
    "_show_command_menu",
    "_split_flags",
    "_usage_to_json",
    "app",
    "console",
]
