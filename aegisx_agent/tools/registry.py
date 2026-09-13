"""Tool registry for managing and invoking tools."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from aegisx_agent.tools.base import Tool, ToolResult, ToolStatus

if TYPE_CHECKING:  # avoids importing the gate (and its audit log) at runtime
    from aegisx_agent.security.permissions import PermissionGate


class ToolRegistry:
    """Central registry for all available tools.

    Every tool call in the project goes through :meth:`execute`, which is also
    where the permission gate is enforced — a path that bypassed the registry
    would bypass the gate, so nothing calls a tool's ``execute`` directly.
    """

    def __init__(self, gate: PermissionGate | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self._gate = gate

    def set_gate(self, gate: PermissionGate | None) -> None:
        """Install the permission gate consulted before every call."""
        self._gate = gate

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Remove a tool by name."""
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def list_tools(self) -> list[Tool]:
        """List all registered tools."""
        return list(self._tools.values())

    def list_schemas(self) -> list[dict[str, Any]]:
        """Get OpenAI function schemas for all tools."""
        return [t.to_schema() for t in self._tools.values()]

    @staticmethod
    def _accepted_params(tool: Tool) -> list[str]:
        """Parameter names declared by the tool's JSON schema."""
        properties = (tool.parameters or {}).get("properties", {})
        return list(properties.keys())

    def _parse_arguments(
        self, tool: Tool, arguments: str | dict[str, Any]
    ) -> dict[str, Any] | ToolResult:
        """Normalise raw arguments into keyword arguments for the tool."""
        if isinstance(arguments, dict):
            args = dict(arguments)
        else:
            raw = arguments.strip()
            if not raw:
                return {}
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                accepted = ", ".join(self._accepted_params(tool)) or "(none)"
                return ToolResult(
                    status=ToolStatus.ERROR,
                    output="",
                    error=(
                        f"Arguments for tool '{tool.name}' are not valid JSON: {raw[:200]!r}. "
                        f"Expected a JSON object with parameters: {accepted}"
                    ),
                )
            args = parsed if isinstance(parsed, dict) else {"input": parsed}

        accepted = self._accepted_params(tool)
        if accepted:
            unknown = [key for key in args if key not in accepted]
            if unknown:
                return ToolResult(
                    status=ToolStatus.ERROR,
                    output="",
                    error=(
                        f"Unknown parameter(s) {unknown} for tool '{tool.name}'. "
                        f"Expected parameters: {', '.join(accepted)}"
                    ),
                )
        return args

    async def execute(self, name: str, arguments: str | dict[str, Any]) -> ToolResult:
        """Execute a tool by name, after the permission gate approves it.

        This method never raises: a missing tool, malformed arguments, a denied
        call, or a crashing tool are all reported back as an error
        ``ToolResult`` so the agent loop can recover instead of aborting the
        whole run.
        """
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                status=ToolStatus.ERROR,
                output="",
                error=f"Tool '{name}' not found. Available tools: {', '.join(self._tools.keys())}",
            )

        args = self._parse_arguments(tool, arguments)
        if isinstance(args, ToolResult):
            return args

        if self._gate is not None:
            decision = await self._gate.check(tool, args)
            if not decision.allowed:
                return ToolResult(
                    status=ToolStatus.ERROR,
                    output="",
                    error=(
                        f"Permission denied for '{name}': {decision.reason}. "
                        "Do not retry this call; ask the user how to proceed instead."
                    ),
                    metadata={
                        "denied": True,
                        "risk": decision.risk.value,
                        "decided_by": decision.decided_by,
                    },
                )

        try:
            return await tool.execute(**args)
        except TypeError as exc:
            accepted = ", ".join(self._accepted_params(tool)) or "(none)"
            return ToolResult(
                status=ToolStatus.ERROR,
                output="",
                error=(
                    f"Invalid arguments for tool '{name}': {exc}. "
                    f"Expected parameters: {accepted}"
                ),
            )
        except SystemExit as exc:
            # SystemExit is not an Exception, so without this a tool could end
            # the whole process instead of failing its own call.
            return ToolResult(
                status=ToolStatus.ERROR,
                output="",
                error=f"Tool '{name}' tried to exit the process (code {exc.code})",
            )
        except Exception as exc:  # noqa: BLE001 - tools must never kill the agent loop
            return ToolResult(
                status=ToolStatus.ERROR,
                output="",
                error=f"Tool '{name}' failed: {type(exc).__name__}: {exc}",
            )
