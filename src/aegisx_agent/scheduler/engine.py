"""Scheduler engine — runs tasks on schedule using the agent.

Inspired by Hermes Agent's cron ticking system.
- Persistent task storage (SQLite)
- Background loop checks for due tasks
- Executes tasks through the agent loop
- Notifies results
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from aegisx_agent.scheduler.task import ScheduleType, ScheduledTask, TaskStatus


class Scheduler:
    """Manages and executes scheduled tasks."""

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
        self._init_db()
        self._load_tasks()

    def _init_db(self) -> None:
        """Initialize SQLite database for task storage."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
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
                metadata TEXT DEFAULT '{}'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                status TEXT NOT NULL,
                result TEXT DEFAULT '',
                duration_seconds REAL DEFAULT 0
            )
        """)
        conn.commit()
        conn.close()

    def _load_tasks(self) -> None:
        """Load all tasks from database."""
        conn = sqlite3.connect(str(self.db_path))
        rows = conn.execute("SELECT * FROM scheduled_tasks").fetchall()
        columns = [desc[0] for desc in conn.execute("SELECT * FROM scheduled_tasks LIMIT 0").description]
        conn.close()

        for row in rows:
            data = dict(zip(columns, row))
            data["metadata"] = json.loads(data.get("metadata", "{}"))
            data["enabled"] = bool(data.get("enabled", 1))
            data["notify"] = bool(data.get("notify", 1))
            task = ScheduledTask.from_dict(data)
            self._tasks[task.id] = task

    def _save_task(self, task: ScheduledTask) -> None:
        """Save a task to database."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            """INSERT OR REPLACE INTO scheduled_tasks
            (id, name, prompt, schedule_type, schedule_value, enabled, status,
             created_at, last_run, last_result, next_run, run_count, error_count,
             persona, model, notify, timeout, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task.id, task.name, task.prompt,
                task.schedule_type.value, task.schedule_value,
                int(task.enabled), task.status.value,
                task.created_at, task.last_run, task.last_result,
                task.next_run, task.run_count, task.error_count,
                task.persona, task.model, int(task.notify),
                task.timeout, json.dumps(task.metadata),
            ),
        )
        conn.commit()
        conn.close()

    def _log_run(self, task_id: str, status: str, result: str, duration: float) -> None:
        """Log a task execution."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT INTO task_logs (task_id, timestamp, status, result, duration_seconds) VALUES (?, ?, ?, ?, ?)",
            (task_id, datetime.now().isoformat(), status, result[:5000], duration),
        )
        conn.commit()
        conn.close()

    # === Public API ===

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
    ) -> ScheduledTask:
        """Add a new scheduled task."""
        task_id = str(uuid.uuid4())[:8]
        schedule = ScheduleType(schedule_type)

        task = ScheduledTask(
            id=task_id,
            name=name,
            prompt=prompt,
            schedule_type=schedule,
            schedule_value=schedule_value,
            persona=persona,
            model=model,
            timeout=timeout,
            notify=notify,
        )
        task.next_run = task.calculate_next_run()
        self._tasks[task_id] = task
        self._save_task(task)
        return task

    def remove_task(self, task_id: str) -> bool:
        """Remove a scheduled task."""
        if task_id not in self._tasks:
            return False
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
        if not task:
            return False
        task.enabled = enabled if enabled is not None else not task.enabled
        self._save_task(task)
        return True

    def get_task(self, task_id: str) -> ScheduledTask | None:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[ScheduledTask]:
        return list(self._tasks.values())

    def get_logs(self, task_id: str, limit: int = 10) -> list[dict[str, Any]]:
        """Get execution logs for a task."""
        conn = sqlite3.connect(str(self.db_path))
        rows = conn.execute(
            "SELECT timestamp, status, result, duration_seconds FROM task_logs "
            "WHERE task_id = ? ORDER BY timestamp DESC LIMIT ?",
            (task_id, limit),
        ).fetchall()
        conn.close()
        return [
            {"timestamp": r[0], "status": r[1], "result": r[2], "duration": r[3]}
            for r in rows
        ]

    def get_due_tasks(self) -> list[ScheduledTask]:
        """Find tasks that are due to run."""
        now = datetime.now()
        due = []
        for task in self._tasks.values():
            if not task.enabled:
                continue
            if task.schedule_type == ScheduleType.ONCE and task.run_count > 0:
                continue
            if task.next_run:
                try:
                    next_dt = datetime.fromisoformat(task.next_run)
                    if next_dt <= now:
                        due.append(task)
                except ValueError:
                    pass
        return due

    async def run_task(self, task: ScheduledTask) -> str:
        """Execute a single task."""
        task.status = TaskStatus.RUNNING
        task.last_run = datetime.now().isoformat()
        self._save_task(task)

        start_time = datetime.now()

        try:
            if not self._agent_factory:
                raise RuntimeError("No agent factory configured")

            agent = self._agent_factory()

            # Override persona if specified
            if task.persona != "default":
                agent.config.persona = task.persona

            # Execute with timeout
            result = await asyncio.wait_for(
                agent.chat(task.prompt),
                timeout=task.timeout,
            )

            task.last_result = result
            task.status = TaskStatus.COMPLETED
            task.run_count += 1
            task.next_run = self._next_run_after(task, successful=True)

            duration = (datetime.now() - start_time).total_seconds()
            self._log_run(task.id, "completed", result, duration)
            self._save_task(task)

            return result

        except asyncio.TimeoutError:
            task.status = TaskStatus.FAILED
            task.last_result = f"Timed out after {task.timeout}s"
            task.error_count += 1
            task.next_run = self._next_run_after(task, successful=False)
            duration = (datetime.now() - start_time).total_seconds()
            self._log_run(task.id, "failed", task.last_result, duration)
            self._save_task(task)
            return task.last_result

        except Exception as e:
            task.status = TaskStatus.FAILED
            task.last_result = f"Error: {type(e).__name__}: {e}"
            task.error_count += 1
            task.next_run = self._next_run_after(task, successful=False)
            duration = (datetime.now() - start_time).total_seconds()
            self._log_run(task.id, "failed", task.last_result, duration)
            self._save_task(task)
            return task.last_result

    def _next_run_after(self, task: ScheduledTask, successful: bool) -> str:
        """Compute the next run time, protecting against retry storms.

        A failed run backs off by ``RETRY_BACKOFF_SECONDS``, and a run that
        outlives its own interval is pushed just past the current moment, so
        ``get_due_tasks`` can never re-fire the same task in a tight loop.
        """
        next_run = task.calculate_next_run()
        if successful:
            floor = datetime.now() + timedelta(seconds=1)
        else:
            floor = datetime.now() + timedelta(seconds=self.RETRY_BACKOFF_SECONDS)

        if not next_run:
            return "" if task.schedule_type == ScheduleType.ONCE else floor.isoformat()
        if datetime.fromisoformat(next_run) <= floor:
            return floor.isoformat()
        return next_run

    async def run_due_tasks(self) -> list[dict[str, Any]]:
        """Run all due tasks. Returns results."""
        due = self.get_due_tasks()
        results = []
        for task in due:
            result = await self.run_task(task)
            results.append({
                "task_id": task.id,
                "name": task.name,
                "status": task.status.value,
                "result": result[:500],
            })
        return results

    async def start_background_loop(self, check_interval: int = 60) -> None:
        """Start background scheduler loop.

        Args:
            check_interval: Seconds between checks for due tasks.
        """
        self._running = True
        while self._running:
            try:
                due = self.get_due_tasks()
                for task in due:
                    await self.run_task(task)
            except Exception:
                pass  # Don't crash the loop on individual task errors
            await asyncio.sleep(check_interval)

    def stop(self) -> None:
        """Stop the background loop."""
        self._running = False
