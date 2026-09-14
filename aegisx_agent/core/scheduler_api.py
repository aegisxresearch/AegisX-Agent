"""Public scheduler operations exposed by :class:`AegisXAgent`."""

from __future__ import annotations

from typing import Any

from aegisx_agent.scheduler.engine import Scheduler


class SchedulerAPI:
    """Mixin containing the agent's scheduled-task API."""

    scheduler: Scheduler

    def add_scheduled_task(
        self,
        name: str,
        prompt: str,
        schedule_type: str,
        schedule_value: str,
        persona: str = "default",
        timeout: int = 120,
        max_retries: int = 3,
    ) -> dict[str, Any]:
        """Add a scheduled task."""
        if max_retries == 3:
            task = self.scheduler.add_task(
                name=name,
                prompt=prompt,
                schedule_type=schedule_type,
                schedule_value=schedule_value,
                persona=persona,
                timeout=timeout,
            )
        else:
            task = self.scheduler.add_task(
                name=name,
                prompt=prompt,
                schedule_type=schedule_type,
                schedule_value=schedule_value,
                persona=persona,
                timeout=timeout,
                max_retries=max_retries,
            )
        return task.to_dict()

    def remove_scheduled_task(self, task_id: str) -> bool:
        """Remove a scheduled task."""
        return self.scheduler.remove_task(task_id)

    def list_scheduled_tasks(self) -> list[dict[str, Any]]:
        """List all scheduled tasks."""
        return [task.to_dict() for task in self.scheduler.list_tasks()]

    def toggle_scheduled_task(self, task_id: str, enabled: bool | None = None) -> bool:
        """Enable or disable a scheduled task."""
        return self.scheduler.toggle_task(task_id, enabled)

    def get_scheduled_task_logs(self, task_id: str, limit: int = 10) -> list[dict[str, Any]]:
        """Get the execution log of a scheduled task."""
        return self.scheduler.get_logs(task_id, limit=limit)

    async def run_scheduled_task_now(self, task_id: str) -> str:
        """Run a scheduled task immediately."""
        task = self.scheduler.get_task(task_id)
        if not task:
            return f"Task {task_id} not found"
        return await self.scheduler.run_task(task)

    async def run_due_scheduled_tasks(self) -> list[dict[str, Any]]:
        """Run all due scheduled tasks."""
        return await self.scheduler.run_due_tasks()

    def request_scheduler_cancel(self, task_id: str) -> bool:
        """Request cancellation of a running or pending scheduled task."""
        return self.scheduler.request_cancel(task_id)

    def resume_scheduled_task(self, task_id: str) -> bool:
        """Resume a paused scheduled task from its persisted checkpoint."""
        return self.scheduler.resume_task(task_id)

    def get_scheduler_checkpoint(self, task_id: str) -> dict[str, Any] | None:
        """Get the latest checkpoint for a scheduled task."""
        return self.scheduler.get_checkpoint(task_id)

    async def start_scheduler(self, check_interval: int = 60) -> None:
        """Start the background scheduler loop."""
        await self.scheduler.start_background_loop(check_interval)
