"""Rich terminal CLI for AegisX.

The Typer application lives in :mod:`aegisx_agent.cli.main`; this package
re-exports the symbols other modules (and the test suite) reach for.
"""

from aegisx_agent.cli.main import (
    CONFIG_FILE,
    _agent,
    _flags_to_schedule,
    _get_agent,
    _get_config,
    _handle_slash_command,
    _permission_prompt,
    _resolve_schedule,
    _split_flags,
    app,
)

__all__ = [
    "CONFIG_FILE",
    "_agent",
    "_flags_to_schedule",
    "_get_agent",
    "_get_config",
    "_handle_slash_command",
    "_permission_prompt",
    "_resolve_schedule",
    "_split_flags",
    "app",
]
