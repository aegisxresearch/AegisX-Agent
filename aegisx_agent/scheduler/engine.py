"""Persistent scheduler with checkpoints and resilient autonomous execution."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from aegisx_agent.scheduler.task import ScheduledTask, ScheduleType, TaskStatus


class Scheduler:
    """Manage scheduled tasks, retries, checkpoints, and background execution."""

    RETRY_BACKOFF_SECONDS = 60

    def __init__(
        self,
        data_dir: Path | str,
        agent_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "scheduler.db"
        self._agent_factory = agent_factory
        self._tasks: dict[str, ScheduledTask] = {}
        self._running = False
        self._active_runs: dict[str, asyncio.Task[str]] = {}
        self._init_db()
        self._load_tasks()

    def _init_db(self) -> None:
        """Initialize SQLite and migrate older task tables in place."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                prompt TEXT NOT NULL,
                schedule_type TEXT NOT NULL,
                schedule_value TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                status TEXT DEFAULT 'pending',
                created_at TEXT NOT NULL,
                last_run TEXT DEFAULT '',
                last_result TEXT DEFAULT '',
                next_run TEXT DEFAULT '',
                run_count INTEGER DEFAULT 0,
                error_count INTEGER DEFAULT 0,
                persona TEXT DEFAULT 'default',
                model TEXT DEFAULT '',
                notify INTEGER DEFAULT 1,
                timeout INTEGER DEFAULT 120,
                metadata TEXT DEFAULT '{}',
                checkpoint TEXT DEFAULT '{}',
                retry_count INTEGER DEFAULT 0,
                max_retries INTEGER DEFAULT 3,
                cancel_requested INTEGER DEFAULT 0
            )
            """
        )
        self._ensure_task_columns(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS task_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                status TEXT NOT NULL,
                result TEXT DEFAULT '',
                duration_seconds REAL DEFAULT 0
            )
            """
        )
        conn.commit()
        conn.close()

    @staticmethod
    def _ensure_task_columns(conn: sqlite3.Connection) -> None:
        """Add autonomous-runtime columns to databases created by older releases."""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(scheduled_tasks)")}
        migrations = {
            "checkpoint": "TEXT DEFAULT '{}'",
            "retry_count": "INTEGER DEFAULT 0",
            "max_retries": "INTEGER DEFAULT 3",
            "cancel_requested": "INTEGER DEFAULT 0",
        }
        for name, definition in migrations.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE scheduled_tasks ADD COLUMN {name} {definition}")

    def _load_tasks(self) -> None:
        """Load tasks and recover runs interrupted by a process restart."""
        conn = sqlite3.connect(str(self.db_path))
        rows = conn.execute("SELECT * FROM scheduled_tasks").fetchall()
        schema_rows = conn.execute("SELECT * FROM scheduled_tasks LIMIT 0")
        columns = [description[0] for description in schema_rows.description]
        conn.close()

        for row in rows:
            data = dict(zip(columns, row))
            data["metadata"] = self._decode_object(data.get("metadata"), {})
            data["checkpoint"] = self._decode_object(data.get("checkpoint"), {})
            data["enabled"] = bool(data.get("enabled", 1))
            data["notify"] = bool(data.get("notify", 1))
            data["cancel_requested"] = bool(data.get("cancel_requested", 0))
            task = ScheduledTask.from_dict(data)
            if task.status is TaskStatus.RUNNING:
                task.status = TaskStatus.PENDING
                task.cancel_requested = False
                task.checkpoint = {
                    **task.checkpoint,
                    "state": "resumed_after_restart",
                    "resumed_at": datetime.now().isoformat(),
                }
            self._tasks[task.id] = task
            if (
                task.status is TaskStatus.PENDING
                and task.checkpoint.get("state") == "resumed_after_restart"
            ):
                self._save_task(task)

    @staticmethod
    def _decode_object(value: Any, default: dict[str, Any]) -> dict[str, Any]:
        """Decode persisted JSON while tolerating old or corrupt optional data."""
        if not value:
            return dict(default)
        try:
            decoded = json.loads(value) if isinstance(value, str) else value
        except (TypeError, json.JSONDecodeError):
            return dict(default)
        return decoded if isinstance(decoded, dict) else dict(default)

    def _save_task(self, task: ScheduledTask) -> None:
        """Persist all task state atomically through one parameterized statement."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            """
            INSERT OR REPLACE INTO scheduled_tasks
            (id, name, prompt, schedule_type, schedule_value, enabled, status,
             created_at, last_run, last_result, next_run, run_count, error_count,
             persona, model, notify, timeout, metadata, checkpoint, retry_count,
             max_retries, cancel_requested)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task.id,
                task.name,
                task.prompt,
                task.schedule_type.value,
                task.schedule_value,
                int(task.enabled),
                task.status.value,
                task.created_at,
                task.last_run,
                task.last_result,
                task.next_run,
                task.run_count,
                task.error_count,
                task.persona,
                task.model,
                int(task.notify),
                task.timeout,
                json.dumps(task.metadata),
                json.dumps(task.checkpoint),
                task.retry_count,
                task.max_retries,
                int(task.cancel_requested),
            ),
        )
        conn.commit()
        conn.close()

    def _log_run(self, task_id: str, status: str, result: str, duration: float) -> None:
        """Log one task execution."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT INTO task_logs "
            "(task_id, timestamp, status, result, duration_seconds) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, datetime.now().isoformat(), status, result[:5000], duration),
        )
        conn.commit()
        conn.close()

    def add_task(
        self,
        name: str,
        prompt: str,
        schedule_type: str,
        schedule_value: str,
        persona: str = "default",
        model: str = "",
        timeout: int = 120,
        notify: bool = True,
        max_retries: int = 3,
    ) -> ScheduledTask:
        """Add a new scheduled task."""
        task = ScheduledTask(
            id=str(uuid.uuid4())[:8],
            name=name,
            prompt=prompt,
            schedule_type=ScheduleType(schedule_type),
            schedule_value=schedule_value,
            persona=persona,
            model=model,
            timeout=timeout,
            notify=notify,
            max_retries=max(0, max_retries),
        )
        task.next_run = task.calculate_next_run()
        self._tasks[task.id] = task
        self._save_task(task)
        return task

    def remove_task(self, task_id: str) -> bool:
        """Remove a task and its execution logs."""
        if task_id not in self._tasks:
            return False
        active_run = self._active_runs.get(task_id)
        if active_run is not None:
            active_run.cancel()
        del self._tasks[task_id]
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
        conn.execute("DELETE FROM task_logs WHERE task_id = ?", (task_id,))
        conn.commit()
        conn.close()
        return True

    def toggle_task(self, task_id: str, enabled: bool | None = None) -> bool:
        """Enable or disable a task."""
        task = self._tasks.get(task_id)
        if task is None:
            return False
        task.enabled = enabled if enabled is not None else not task.enabled
        self._save_task(task)
        return True

    def request_cancel(self, task_id: str) -> bool:
        """Request and, when active, immediately signal cooperative cancellation."""
        task = self._tasks.get(task_id)
        if task is None:
            return False
        task.cancel_requested = True
        task.checkpoint = {**task.checkpoint, "state": "cancellation_requested"}
        active_run = self._active_runs.get(task_id)
        if active_run is not None:
            active_run.cancel()
        self._save_task(task)
        return True

    def resume_task(self, task_id: str) -> bool:
        """Resume a paused task from its persisted checkpoint."""
        task = self._tasks.get(task_id)
        if task is None:
            return False
        task.cancel_requested = False
        task.status = TaskStatus.PENDING
        task.next_run = datetime.now().isoformat()
        task.checkpoint = {**task.checkpoint, "state": "resumed"}
        self._save_task(task)
        return True

    def get_checkpoint(self, task_id: str) -> dict[str, Any] | None:
        """Return a copy of the latest task checkpoint."""
        task = self._tasks.get(task_id)
        return dict(task.checkpoint) if task else None

    def get_task(self, task_id: str) -> ScheduledTask | None:
        """Get a task by ID."""
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[ScheduledTask]:
        """List all tasks."""
        return list(self._tasks.values())

    def get_logs(self, task_id: str, limit: int = 10) -> list[dict[str, Any]]:
        """Get execution logs for a task."""
        conn = sqlite3.connect(str(self.db_path))
        rows = conn.execute(
            "SELECT timestamp, status, result, duration_seconds "
            "FROM task_logs WHERE task_id = ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (task_id, limit),
        ).fetchall()
        conn.close()
        return [
            {"timestamp": row[0], "status": row[1], "result": row[2], "duration": row[3]}
            for row in rows
        ]

    def get_due_tasks(self) -> list[ScheduledTask]:
        """Find enabled tasks whose next run is due."""
        now = datetime.now()
        due = []
        for task in self._tasks.values():
            if not task.enabled or task.cancel_requested:
                continue
            if task.status is TaskStatus.PAUSED:
                continue
            if task.schedule_type is ScheduleType.ONCE and task.run_count > 0:
                continue
            if task.next_run:
                try:
                    if datetime.fromisoformat(task.next_run) <= now:
                        due.append(task)
                except ValueError:
                    pass
        return due

    async def run_task(self, task: ScheduledTask) -> str:
        """Execute one task and persist a checkpoint at each terminal state."""
        if task.cancel_requested:
            task.status = TaskStatus.PAUSED
            task.checkpoint = {**task.checkpoint, "state": "cancelled_before_start"}
            self._save_task(task)
            return "Cancelled"

        task.status = TaskStatus.RUNNING
        task.last_run = datetime.now().isoformat()
        task.checkpoint = {
            **task.checkpoint,
            "state": "running",
            "attempt": task.retry_count + 1,
            "started_at": task.last_run,
        }
        self._save_task(task)
        start_time = datetime.now()

        run = asyncio.create_task(self._run_task_guarded(task))
        self._active_runs[task.id] = run
        try:
            result = await run
        except asyncio.CancelledError:
            task.status = TaskStatus.PAUSED
            task.cancel_requested = True
            task.next_run = ""
            task.checkpoint = {
                **task.checkpoint,
                "state": "cancelled",
                "cancelled_at": datetime.now().isoformat(),
            }
            self._save_task(task)
            if task.cancel_requested:
                return "Cancelled"
            raise
        except asyncio.TimeoutError:
            return self._record_failure(task, f"Timed out after {task.timeout}s", start_time)
        except Exception as exc:
            return self._record_failure(
                task, f"Error: {type(exc).__name__}: {exc}", start_time
            )
        finally:
            self._active_runs.pop(task.id, None)

        if task.cancel_requested:
            task.status = TaskStatus.PAUSED
            task.next_run = ""
            task.checkpoint = {
                **task.checkpoint,
                "state": "cancelled",
                "completed_at": datetime.now().isoformat(),
            }
            self._save_task(task)
            return "Cancelled"

        task.last_result = result
        task.status = TaskStatus.COMPLETED
        task.run_count += 1
        task.retry_count = 0
        task.metadata.pop("failure_fingerprint", None)
        task.metadata.pop("failure_streak", None)
        task.checkpoint = {
            **task.checkpoint,
            "state": "completed",
            "completed_at": datetime.now().isoformat(),
        }
        task.next_run = self._next_run_after(task, successful=True)
        duration = (datetime.now() - start_time).total_seconds()
        self._log_run(task.id, "completed", result, duration)
        self._save_task(task)
        return result

    async def _run_task_guarded(self, task: ScheduledTask) -> str:
        """Create the task agent and run it under the configured timeout."""
        if self._agent_factory is None:
            raise RuntimeError("No agent factory configured")
        agent = self._agent_factory()
        if task.persona != "default":
            agent.config.persona = task.persona
        return await asyncio.wait_for(agent.chat(task.prompt), timeout=task.timeout)

    def _record_failure(self, task: ScheduledTask, message: str, start_time: datetime) -> str:
        """Persist failure state, apply exponential backoff, and detect loops."""
        task.status = TaskStatus.FAILED
        task.last_result = message
        task.error_count += 1
        task.retry_count += 1
        fingerprint = message[:500]
        previous = task.metadata.get("failure_fingerprint")
        streak = int(task.metadata.get("failure_streak", 0)) + 1 if previous == fingerprint else 1
        task.metadata["failure_fingerprint"] = fingerprint
        task.metadata["failure_streak"] = streak
        task.checkpoint = {
            **task.checkpoint,
            "state": "paused" if streak > task.max_retries else "failed",
            "error": message,
            "failed_at": datetime.now().isoformat(),
        }
        if streak > task.max_retries:
            task.status = TaskStatus.PAUSED
            task.next_run = ""
        else:
            task.next_run = self._next_run_after(task, successful=False)
        duration = (datetime.now() - start_time).total_seconds()
        self._log_run(task.id, "failed", message, duration)
        self._save_task(task)
        return message

    def _next_run_after(self, task: ScheduledTask, successful: bool) -> str:
        """Compute the next run while enforcing a future scheduling floor."""
        next_run = task.calculate_next_run()
        if successful:
            floor = datetime.now() + timedelta(seconds=1)
        else:
            backoff = self.RETRY_BACKOFF_SECONDS * (2 ** max(0, task.retry_count - 1))
            floor = datetime.now() + timedelta(seconds=backoff)
        if not next_run:
            return "" if task.schedule_type is ScheduleType.ONCE else floor.isoformat()
        if datetime.fromisoformat(next_run) <= floor:
            return floor.isoformat()
        return next_run

    async def run_due_tasks(self) -> list[dict[str, Any]]:
        """Run all currently due tasks and return compact results."""
        results = []
        for task in self.get_due_tasks():
            result = await self.run_task(task)
            results.append({
                "task_id": task.id,
                "name": task.name,
                "status": task.status.value,
                "result": result[:500],
            })
        return results

    async def start_background_loop(self, check_interval: int = 60) -> None:
        """Poll and execute due tasks until :meth:`stop` is called."""
        self._running = True
        while self._running:
            try:
                for task in self.get_due_tasks():
                    await self.run_task(task)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(check_interval)

    def stop(self) -> None:
        """Stop the background scheduler loop."""
        self._running = False


__all__ = ["Scheduler"]
