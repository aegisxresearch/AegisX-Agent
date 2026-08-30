"""Calculator tool — evaluate mathematical expressions."""

from __future__ import annotations

import math
from typing import Any

from utopia_agent.tools.base import Tool, ToolResult, ToolStatus


class CalculatorTool(Tool):
    """Evaluate mathematical expressions safely."""

    def __init__(self) -> None:
        super().__init__(
            name="calculator",
            description=(
                "Evaluate mathematical expressions. Supports basic arithmetic, "
                "trigonometry, logarithms, and common math functions. "
                "Examples: 2+2, sqrt(144), sin(pi/2), log(100), 2**10"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "Mathematical expression to evaluate",
                    }
                },
                "required": ["expression"],
            },
        )
        # Safe math namespace
        self._namespace: dict[str, Any] = {
            "abs": abs,
            "round": round,
            "min": min,
            "max": max,
            "sum": sum,
            "len": len,
            "pow": pow,
            "sqrt": math.sqrt,
            "sin": math.sin,
            "cos": math.cos,
            "tan": math.tan,
            "asin": math.asin,
            "acos": math.acos,
            "atan": math.atan,
            "atan2": math.atan2,
            "log": math.log,
            "log2": math.log2,
            "log10": math.log10,
            "exp": math.exp,
            "ceil": math.ceil,
            "floor": math.floor,
            "factorial": math.factorial,
            "gcd": math.gcd,
            "pi": math.pi,
            "e": math.e,
            "tau": math.tau,
            "inf": math.inf,
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        expression = kwargs.get("expression", "")
        if not expression.strip():
            return ToolResult(status=ToolStatus.ERROR, output="", error="No expression provided")

        try:
            # Use eval with restricted namespace for safety
            result = eval(expression, {"__builtins__": {}}, self._namespace)  # noqa: S307
            return ToolResult(
                status=ToolStatus.SUCCESS,
                output=f"{expression} = {result}",
                metadata={"expression": expression, "result": str(result)},
            )
        except Exception as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                output="",
                error=f"Math error: {type(e).__name__}: {e}",
            )
