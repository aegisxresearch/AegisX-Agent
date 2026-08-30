"""Date and time utility tool."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from utopia_agent.tools.base import Tool, ToolResult, ToolStatus


class DateTimeTool(Tool):
    """Get current date/time and perform date calculations."""

    def __init__(self) -> None:
        super().__init__(
            name="datetime",
            description=(
                "Get current date and time, or calculate date differences. "
                "Returns current timestamp, timezone info, and can compute "
                "differences between dates."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["now", "diff"],
                        "description": "'now' for current time, 'diff' for date difference",
                        "default": "now",
                    },
                    "date1": {
                        "type": "string",
                        "description": "First date (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)",
                    },
                    "date2": {
                        "type": "string",
                        "description": "Second date for diff calculation",
                    },
                },
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = kwargs.get("action", "now")

        try:
            if action == "now":
                now = datetime.now(timezone.utc)
                local = datetime.now()
                output = (
                    f"UTC Time:      {now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                    f"Local Time:    {local.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"Date:          {local.strftime('%A, %B %d, %Y')}\n"
                    f"Unix Timestamp: {int(now.timestamp())}"
                )
                return ToolResult(status=ToolStatus.SUCCESS, output=output)

            elif action == "diff":
                d1_str = kwargs.get("date1", "")
                d2_str = kwargs.get("date2", "")
                if not d1_str or not d2_str:
                    return ToolResult(
                        status=ToolStatus.ERROR,
                        output="",
                        error="Both date1 and date2 are required for diff",
                    )

                d1 = self._parse_date(d1_str)
                d2 = self._parse_date(d2_str)
                delta = d2 - d1

                days = delta.days
                hours, remainder = divmod(abs(delta.seconds), 3600)
                minutes, seconds = divmod(remainder, 60)

                output = (
                    f"Date 1: {d1.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"Date 2: {d2.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"---\n"
                    f"Difference: {days} days, {hours} hours, {minutes} minutes, {seconds} seconds\n"
                    f"Total days: {days}\n"
                    f"Total hours: {delta.total_seconds() / 3600:.2f}"
                )
                return ToolResult(status=ToolStatus.SUCCESS, output=output)

            else:
                return ToolResult(
                    status=ToolStatus.ERROR, output="", error=f"Unknown action: {action}"
                )
        except Exception as e:
            return ToolResult(status=ToolStatus.ERROR, output="", error=str(e))

    @staticmethod
    def _parse_date(s: str) -> datetime:
        formats = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]
        for fmt in formats:
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        raise ValueError(f"Cannot parse date: {s}. Use YYYY-MM-DD or YYYY-MM-DD HH:MM:SS")
