"""Persistent scheduler daemon: cron-driven autonomous execution.

``aegisx daemon run`` starts a long-lived process that polls the scheduler
SQLite store for due tasks and executes them unattended. Tasks live in the
database, not in the chat process, so a daemon started later picks up
everything scheduled before it — the missing piece between "the scheduler
works in-process" and "cron for agents".

Shutdown is cooperative: SIGINT/SIGTERM flip a stop event, the loop finishes
its current poll cycle and stops scheduling new runs. A run interrupted by a
killed daemon is recovered as ``resumed_after_restart`` on the next start.
"""

from __future__ import annotations

import asyncio
import signal
from datetime import datetime, timedelta
from typing import Any

from aegisx_agent.scheduler.engine import Scheduler

DEFAULT_POLL_SECONDS = 30.0


class SchedulerDaemon:
    """Long-lived scheduler process with signal-safe cooperative shutdown."""

    def __init__(
        self,
        scheduler: Scheduler,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        catch_up_limit: int = 5,
    ) -> None:
        self.scheduler = scheduler
        self.poll_seconds = poll_seconds
        self.catch_up_limit = cap = max(1, catch_up_limit)
        self._stop = asyncio.Event()
        self._stats = {"cycles": 0, "runs": 0, "failures": 0, "catch_up_skips": 0}
        self._cap = cap
        self._tasks: list[asyncio.Task[dict[str, Any]]] = []

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def request_stop(self) -> None:
        """Flip the stop flag; the loop exits after the current cycle."""
        self._stop.set()

    def install_signal_handlers(self) -> None:
        """Stop cleanly on SIGINT/SIGTERM instead of dying mid-run."""

        def _handle(signum: int, _frame: Any) -> None:
            self.request_stop()
            signal.signal(signal.SIGINT, signal.SIG_DFL)

        signal.signal(signal.SIGINT, _handle)
        signal.signal(signal.SIGTERM, _handle)

    async def run_forever(self) -> None:
        """Poll for due tasks until :meth:`request_stop` is called.

        The daemon does not need a full agent at startup — tasks are loaded
        from SQLite, and agents are built lazily per run by the factory, so
        the daemon runs even without LLM credentials until a task fires.
        """
        try:
            while not self._stop.is_set():
                started = datetime.now()
                try:
                    await self._cycle()
                except Exception as exc:  # noqa: BLE001 - the daemon must survive
                    self._stats["failures"] += 1
                    print(
                        f"[aegisx-daemon] cycle error: {type(exc).__name__}: {exc}",
                        flush=True,
                    )
                self._stats["cycles"] += 1
                # Sleep in short slices so a stop request is honored quickly.
                slept = 0.0
                while slept < self.poll_seconds and not self._stop.is_set():
                    slice_ = self_poll_slice(self.poll_seconds - slept)
                    await asyncio.sleep(slice_)
                    slept += slice_
                _ = started  # kept for future cycle-duration metrics
        finally:
            await self._drain_active()

    async def run_for(self, seconds: float) -> None:
        """Run the loop for a bounded time (used by tests and smoke runs)."""
        run_task = asyncio.create_task(self.run_forever())
        await asyncio.sleep(seconds)
        self.request_stop()
        await asyncio.wait_for(run_task, timeout=15)

    async def _drain_active(self) -> None:
        """Give in-flight runs a grace period to finish before exiting."""
        active = [task for task in self._tasks if not task.done()]
        if not active:
            return
        await asyncio.wait(self._tasks, timeout=10)

    # ------------------------------------------------------------------ #
    # The poll cycle
    # -- ---------------------------------------------------------------- #

    async def _cycle(self) -> None:
        """One poll: execute due tasks (bounded) and prune finished runs."""
        self._prune()
        due = self.scheduler.get_due_tasks()
        if len(due) > self._cap:
            self._stats["catch_up_skips"] += len(due) - self._cap
            due = due[: self._cap]
        for task in due:
            self._stats["runs"] += 1
            result = await self.scheduler.run_task(task)
            if task.status.value == "failed":
                self._stats["failures"] += 1
            print(
                f"[aegisx-daemon] {task.id[:8]} '{task.name}' -> "
                f"{task.status.value}: {result[:120]}",
                flush=True,
            )

    def _prune(self) -> None:
        """Drop finished asyncio tasks so the active list cannot grow."""
        self._tasks = [task for task in self._tasks if not task.done()]

    # ------------------------------------------------------------------ #
    # Reporting
    # ------------------------------------------------------------------ #

    def status(self) -> dict[str, Any]:
        """A snapshot for the ``aegisx daemon status`` display."""
        tasks = self.scheduler.list_tasks()
        return {
            "poll_seconds": self.poll_seconds,
            "catch_up_limit": self.catch_up_limit,
            "stop_requested": self._stop.is_set(),
            **self._stats,
            "tasks": len(tasks),
            "enabled": sum(1 for task in tasks if task.enabled),
            "next_run": min(
                (task.next_run for task in tasks if task.enabled and task.next_run),
                default="",
            ),
            "due_now": len(self.scheduler.get_due_tasks()),
        }


def self_poll_slice(remaining: float) -> float:
    """Clamp one sleep slice to sane bounds (used by the stop-aware sleep)."""
    return max(0.1, min(0.5, remaining))


def format_status(status: dict[str, Any]) -> str:
    """One-line daemon status for logs and the CLI."""
    next_run = status.get("next_run") or "-"
    return (
        f"poll={status['poll_seconds']:g}s tasks={status['tasks']} "
        f"enabled={status['enabled']} due_now={status['due_now']} "
        f"runs={status['runs']} failures={status['failures']} "
        f"cycles={status['cycles']} next={next_run}"
    )


def catch_up_window(limit: int, poll_seconds: float) -> timedelta:
    """The window a single cycle can realistically cover (diagnostics)."""
    return timedelta(seconds=limit * poll_seconds)


__all__ = [
    "DEFAULT_POLL_SECONDS",
    "SchedulerDaemon",
    "catch_up_window",
    "format_status",
    "self_poll_slice",
]
