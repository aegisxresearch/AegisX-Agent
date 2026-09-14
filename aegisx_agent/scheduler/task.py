"""Scheduled task data model."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from aegisx_agent.scheduler.cron import next_fire as cron_next_fire


class TaskStatus(str, Enum):
    """Task execution status."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


class ScheduleType(str, Enum):
    """How often the task runs."""
    ONCE = "once"           # Run once at specific time
    INTERVAL = "interval"   # Run every N minutes/hours
    DAILY = "daily"         # Run once per day
    WEEKLY = "weekly"       # Run once per week
    CRON = "cron"           # Full cron expression


@dataclass
class ScheduledTask:
    """A task that runs automatically on a schedule."""

    id: str
    name: str
    prompt: str                          # What to tell the agent to do
    schedule_type: ScheduleType
    schedule_value: str = ""              # Cron expr, interval string, or time
    enabled: bool = True
    status: TaskStatus = TaskStatus.PENDING
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_run: str = ""
    last_result: str = ""
    next_run: str = ""
    run_count: int = 0
    error_count: int = 0
    persona: str = "default"            # Which persona to use
    model: str = ""                      # Override model (optional)
    notify: bool = True                  # Print result when done
    timeout: int = 120                   # Max seconds per run
    metadata: dict[str, Any] = field(default_factory=dict)
    checkpoint: dict[str, Any] = field(default_factory=dict)
    retry_count: int = 0
    max_retries: int = 3
    cancel_requested: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "prompt": self.prompt,
            "schedule_type": self.schedule_type.value,
            "schedule_value": self.schedule_value,
            "enabled": self.enabled,
            "status": self.status.value,
            "created_at": self.created_at,
            "last_run": self.last_run,
            "last_result": self.last_result,
            "next_run": self.next_run,
            "run_count": self.run_count,
            "error_count": self.error_count,
            "persona": self.persona,
            "model": self.model,
            "notify": self.notify,
            "timeout": self.timeout,
            "metadata": self.metadata,
            "checkpoint": self.checkpoint,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "cancel_requested": self.cancel_requested,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScheduledTask:
        """Restore a task while tolerating records from older schema versions."""
        restored = dict(data)
        restored["schedule_type"] = ScheduleType(restored.get("schedule_type", "interval"))
        restored["status"] = TaskStatus(restored.get("status", "pending"))
        restored.setdefault("checkpoint", {})
        restored.setdefault("retry_count", 0)
        restored.setdefault("max_retries", 3)
        restored.setdefault("cancel_requested", False)
        return cls(**{
            key: value for key, value in restored.items() if key in cls.__dataclass_fields__
        })

    def calculate_next_run(self) -> str:
        """Calculate when this task should next run."""
        now = datetime.now()

        match self.schedule_type:
            case ScheduleType.ONCE:
                return ""  # No next run

            case ScheduleType.INTERVAL:
                # Parse "30m", "2h", "1d"
                delta = self._parse_interval(self.schedule_value)
                if delta:
                    from datetime import timedelta
                    next_dt = now + timedelta(seconds=delta)
                    return next_dt.isoformat()

            case ScheduleType.DAILY:
                # Parse "HH:MM" format
                try:
                    hour, minute = map(int, self.schedule_value.split(":"))
                    next_dt = now.replace(hour=hour, minute=minute, second=0)
                    if next_dt <= now:
                        from datetime import timedelta
                        next_dt += timedelta(days=1)
                    return next_dt.isoformat()
                except ValueError:
                    pass

            case ScheduleType.WEEKLY:
                # Parse "MON:HH:MM" or just "HH:MM"
                try:
                    parts = self.schedule_value.split(":")
                    if len(parts) == 3:
                        day_map = {
                            "MON": 0, "TUE": 1, "WED": 2, "THU": 3,
                            "FRI": 4, "SAT": 5, "SUN": 6,
                        }
                        target_day = day_map.get(parts[0].upper(), 0)
                        hour, minute = int(parts[1]), int(parts[2])
                    else:
                        target_day = now.weekday()
                        hour, minute = int(parts[0]), int(parts[1])

                    from datetime import timedelta
                    days_ahead = (target_day - now.weekday()) % 7
                    if days_ahead == 0:
                        next_dt = now.replace(hour=hour, minute=minute, second=0)
                        if next_dt <= now:
                            next_dt += timedelta(days=7)
                    else:
                        next_dt = now.replace(
                            hour=hour, minute=minute, second=0
                        ) + timedelta(days=days_ahead)
                    return next_dt.isoformat()
                except (ValueError, KeyError):
                    pass

            case ScheduleType.CRON:
                # Simple cron: "*/5 * * * *" = every 5 minutes
                # For simplicity, parse common patterns
                return self._parse_cron(self.schedule_value, now)

        return ""

    @staticmethod
    def _parse_interval(value: str) -> int | None:
        """Parse interval like '30m', '2h', '1d' to seconds."""
        value = value.strip().lower()
        multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        for suffix, mult in multipliers.items():
            if value.endswith(suffix):
                try:
                    return int(value[:-1]) * mult
                except ValueError:
                    return None
        # Try plain number as minutes
        try:
            return int(value) * 60
        except ValueError:
            return None

    @staticmethod
    def _parse_cron(expr: str, now: datetime) -> str:
        """Standard 5-field cron parsing (ranges, steps, lists, names).

        ``*/15 * * * *`` lands on real :00/:15/:30/:45 boundaries instead of
        drifting from the scheduling moment, and unparseable expressions
        return ``""`` so the task is surfaced as misconfigured rather than
        silently never firing.
        """
        fired = cron_next_fire(expr, now)
        return fired.isoformat() if fired else ""
