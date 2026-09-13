"""Append-only audit trail for tool permission decisions.

Every call the gate sees is written as one JSON line, so an unattended run can
be reconstructed afterwards: which tool ran, on what, how risky it was, and who
let it through. Values are redacted on the way to disk — a tool argument is not
allowed to leak an API key into a log file.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

#: Marker written in place of a sensitive value.
REDACTED = "[redacted]"

#: Strings longer than this are truncated (code, HTML and tool output are often huge).
MAX_STRING = 300

_SENSITIVE_TOKENS = (
    "apikey",
    "token",
    "password",
    "passwd",
    "secret",
    "authorization",
    "cookie",
    "credential",
    "privatekey",
    "sessionid",
)
_SENSITIVE_EXACT = {"key", "keys", "auth", "pwd", "bearer"}


def _is_sensitive(key: str) -> bool:
    """Whether an argument name looks like it carries a credential."""
    normalized = key.lower().replace("_", "").replace("-", "")
    if normalized in _SENSITIVE_EXACT:
        return True
    return any(token in normalized for token in _SENSITIVE_TOKENS)


def redact(value: Any, key: str = "") -> Any:
    """Return ``value`` with credentials replaced and long strings truncated.

    Recurses through dicts and lists so a header such as
    ``{"Authorization": "Bearer ..."}`` nested inside an ``api_call`` body is
    caught as well.
    """
    if key and _is_sensitive(key):
        return REDACTED
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str) and len(value) > MAX_STRING:
        return f"{value[:MAX_STRING]}… [{len(value)} chars]"
    return value


class AuditLog:
    """Append-only JSONL log of tool decisions.

    Auditing is best effort on purpose: a full disk must not take the agent
    down mid-task. The failure is recorded on :attr:`last_error` instead of
    being swallowed silently.
    """

    def __init__(self, path: Path | str | None, enabled: bool = True) -> None:
        self.path = Path(path).expanduser() if path else None
        self.enabled = bool(enabled and self.path is not None)
        self.last_error = ""

    def record(self, event: str, **fields: Any) -> dict[str, Any] | None:
        """Append one event. Returns the entry, or ``None`` when not written."""
        if not self.enabled or self.path is None:
            return None

        entry: dict[str, Any] = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "event": event,
            **redact(fields),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None
        return entry

    def tail(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return up to ``limit`` most recent entries (oldest first)."""
        if not self.enabled or self.path is None or not self.path.exists():
            return []

        entries: list[dict[str, Any]] = []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        parsed = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(parsed, dict):
                        entries.append(parsed)
        except OSError as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []

        return entries[-limit:]
