"""Token-usage tracking: one JSONL line per agent run.

The tracker wraps the agent's LLM provider, so **every** path through the
agent — chat, streaming chat, plan execution, scheduled runs, future
subagents — records what it consumed. Lines carry a run id so a scheduler
run's cost can be attributed even though runs share the process.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aegisx_agent.llm.base import LLMProvider, LLMResponse

USAGE_FILE = "usage.jsonl"


class UsageTracker:
    """Tracks tokens and calls by wrapping an LLM provider.

    The wrapper is transparent: ``agent.llm`` (and therefore the agentic loop)
    keeps working exactly as before, while every response's usage dict is
    appended to ``<data_dir>/usage.jsonl`` under an attributed run id.
    """

    def __init__(self, inner: LLMProvider, path: Path | str) -> None:
        self._inner = inner
        self.path = Path(path).expanduser()
        self.run_id = uuid.uuid4().hex[:12]
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0

    # --- Transparent provider surface ---------------------------------- #

    @property
    def model(self) -> str:
        """The wrapped provider's model name."""
        return self._inner.model

    async def chat(self, messages: Any, **kwargs: Any) -> Any:
        """Forward one completion call and record its usage."""
        response = await self._inner.chat(messages, **kwargs)
        self._record(getattr(response, "usage", {}) or {})
        return response

    def stream_chat(self, messages: Any, **kwargs: Any) -> Any:
        """Forward a streaming call, recording the final response's usage."""
        return self._streaming(messages, **kwargs)

    async def _streaming(self, messages: Any, **kwargs: Any) -> Any:
        async for item in self._inner.stream_chat(messages, **kwargs):
            if isinstance(item, LLMResponse):
                self._record(item.usage or {})
            yield item

    async def run_streaming(self, messages: Any, **kwargs: Any) -> Any:
        """Forward the single-request streaming path and record usage."""
        response = await self._inner.run_streaming(messages, **kwargs)
        self._record(getattr(response, "usage", {}) or {})
        return response

    def __getattr__(self, name: str) -> Any:
        """Delegate everything else (config, provider-specific attributes)."""
        return getattr(self._inner, name)

    # --- Recording ------------------------------------------------------ #

    def _record(self, usage: dict[str, Any]) -> None:
        """Accumulate one response's usage and append a JSONL line."""
        input_tokens = int(usage.get("prompt_tokens", 0) or 0) + int(
            usage.get("input_tokens", 0) or 0
        )
        output_tokens = int(usage.get("completion_tokens", 0) or 0) + int(
            usage.get("output_tokens", 0) or 0
        )
        total = int(usage.get("total_tokens", 0) or 0) or (input_tokens + output_tokens)
        self.calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.total_tokens += total

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "model": self.model,
            "calls": 1,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            # Observability must never take a run down; the in-memory totals
            # above are still accurate for this process.
            pass

    # --- Summaries ------------------------------------------------------ #

    def summarize(
        self,
        entries: list[dict[str, Any]] | None = None,
        run_id: str | None = None,
        since: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate usage entries, optionally filtered by run and/or date."""
        rows = entries if entries is not None else read_usage(self.path)
        if run_id:
            rows = [row for row in rows if row.get("run_id") == run_id]
        if since:
            rows = [row for row in rows if str(row.get("timestamp", "")) >= since]
        aggregate = {
            "calls": sum(int(row.get("calls", 0) or 0) for row in rows),
            "input_tokens": sum(int(row.get("input_tokens", 0) or 0) for row in rows),
            "output_tokens": sum(int(row.get("output_tokens", 0) or 0) for row in rows),
            "total_tokens": sum(int(row.get("total_tokens", 0) or 0) for row in rows),
        }
        per_model: dict[str, int] = {}
        for row in rows:
            model = str(row.get("model", "unknown"))
            per_model[model] = per_model.get(model, 0) + int(row.get("total_tokens", 0) or 0)
        return {
            **aggregate,
            "models": per_model,
            "entries": len(rows),
            "run_id": run_id,
            "since": since,
        }


def read_usage(path: Path | str) -> list[dict[str, Any]]:
    """Read the usage JSONL file, skipping corrupt lines."""
    usage_path = Path(path).expanduser()
    if not usage_path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with usage_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    rows.append(parsed)
    except OSError:
        return []
    return rows


def parse_natural_time(reference: str) -> str | None:
    """Translate ``today`` / ``yesterday`` / ``Nd`` / ``Nh`` to an ISO cutoff."""
    text = reference.strip().lower()
    now = datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if text == "today":
        return midnight.isoformat()
    if text == "yesterday":
        return (midnight - timedelta(days=1)).isoformat()
    if text.endswith("d") and text[:-1].isdigit():
        return (now - timedelta(days=int(text[:-1]))).isoformat()
    if text.endswith("h") and text[:-1].isdigit():
        return (now - timedelta(hours=int(text[:-1]))).isoformat()
    return None


__all__ = ["USAGE_FILE", "UsageTracker", "parse_natural_time", "read_usage"]
