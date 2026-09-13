"""Scheduler: due-task selection and real execution through a fake agent."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from support import run

from aegisx_agent.scheduler.engine import Scheduler
from aegisx_agent.scheduler.task import ScheduledTask, ScheduleType, TaskStatus


class FakeAgent:
    """Minimal stand-in for AegisXAgent so runs never touch a network."""

    def __init__(
        self, reply: str = "ok", delay: float = 0.0, error: Exception | None = None
    ) -> None:
        self.reply = reply
        self.delay = delay
        self.error = error
        self.prompts: list[str] = []
        self.config = SimpleNamespace(persona="default")

    async def chat(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.reply


def _scheduler(tmp_path, agent: FakeAgent) -> Scheduler:
    return Scheduler(data_dir=tmp_path, agent_factory=lambda: agent)


def _make_due(scheduler: Scheduler, task_id: str) -> None:
    """Move a task's next run into the past so it becomes due."""
    task = scheduler.get_task(task_id)
    assert task is not None
    task.next_run = (datetime.now() - timedelta(minutes=1)).isoformat()


def test_add_and_list_persist_across_instances(tmp_path) -> None:
    scheduler = _scheduler(tmp_path, FakeAgent())
    task = scheduler.add_task("nightly", "summarise the inbox", "interval", "30m")

    assert task.next_run
    assert [entry.id for entry in scheduler.list_tasks()] == [task.id]

    reloaded = _scheduler(tmp_path, FakeAgent())
    stored = reloaded.get_task(task.id)

    assert stored is not None
    assert stored.name == "nightly"
    assert stored.prompt == "summarise the inbox"
    assert stored.schedule_value == "30m"


def test_parse_interval_units() -> None:
    assert ScheduledTask._parse_interval("15s") == 15
    assert ScheduledTask._parse_interval("30m") == 1800
    assert ScheduledTask._parse_interval("2h") == 7200
    assert ScheduledTask._parse_interval("1d") == 86400
    assert ScheduledTask._parse_interval("10") == 600
    assert ScheduledTask._parse_interval("nonsense") is None
    assert ScheduledTask._parse_interval("xm") is None  # valid suffix, bad number


def test_calculate_next_run_variants() -> None:
    now = datetime.now()

    interval = ScheduledTask(
        id="1", name="i", prompt="p", schedule_type=ScheduleType.INTERVAL, schedule_value="1h"
    )
    assert datetime.fromisoformat(interval.calculate_next_run()) > now

    daily = ScheduledTask(
        id="2", name="d", prompt="p", schedule_type=ScheduleType.DAILY, schedule_value="09:00"
    )
    assert datetime.fromisoformat(daily.calculate_next_run()) > now

    cron = ScheduledTask(
        id="3",
        name="c",
        prompt="p",
        schedule_type=ScheduleType.CRON,
        schedule_value="*/15 * * * *",
    )
    assert datetime.fromisoformat(cron.calculate_next_run()) > now

    once = ScheduledTask(
        id="4", name="o", prompt="p", schedule_type=ScheduleType.ONCE, schedule_value=""
    )
    assert once.calculate_next_run() == ""


def test_due_selection_respects_time_and_enabled_flag(tmp_path) -> None:
    scheduler = _scheduler(tmp_path, FakeAgent())
    task = scheduler.add_task("nightly", "do it", "interval", "1h")

    assert scheduler.get_due_tasks() == []

    _make_due(scheduler, task.id)
    assert [entry.id for entry in scheduler.get_due_tasks()] == [task.id]

    scheduler.toggle_task(task.id, False)
    assert scheduler.get_due_tasks() == []

    scheduler.toggle_task(task.id, True)
    assert [entry.id for entry in scheduler.get_due_tasks()] == [task.id]


def test_due_task_actually_runs(tmp_path) -> None:
    agent = FakeAgent(reply="summary ready")
    scheduler = _scheduler(tmp_path, agent)
    task = scheduler.add_task("nightly", "summarise the inbox", "interval", "30m")
    _make_due(scheduler, task.id)

    results = run(scheduler.run_due_tasks())

    assert results == [
        {
            "task_id": task.id,
            "name": "nightly",
            "status": "completed",
            "result": "summary ready",
        }
    ]
    assert agent.prompts == ["summarise the inbox"]

    stored = scheduler.get_task(task.id)
    assert stored is not None
    assert stored.status is TaskStatus.COMPLETED
    assert stored.run_count == 1
    assert stored.last_result == "summary ready"
    assert datetime.fromisoformat(stored.next_run) > datetime.now()

    logs = scheduler.get_logs(task.id)
    assert len(logs) == 1
    assert logs[0]["status"] == "completed"


def test_once_task_is_not_due_again(tmp_path) -> None:
    agent = FakeAgent()
    scheduler = _scheduler(tmp_path, agent)
    task = scheduler.add_task("one shot", "do it once", "once", "")
    _make_due(scheduler, task.id)

    run(scheduler.run_due_tasks())

    assert scheduler.get_due_tasks() == []
    stored = scheduler.get_task(task.id)
    assert stored is not None
    assert stored.run_count == 1


def test_failing_task_records_the_error(tmp_path) -> None:
    agent = FakeAgent(error=RuntimeError("tool exploded"))
    scheduler = _scheduler(tmp_path, agent)
    task = scheduler.add_task("nightly", "do it", "interval", "30m")
    _make_due(scheduler, task.id)

    run(scheduler.run_due_tasks())

    stored = scheduler.get_task(task.id)
    assert stored is not None
    assert stored.status is TaskStatus.FAILED
    assert stored.error_count == 1
    assert "tool exploded" in stored.last_result
    assert scheduler.get_logs(task.id)[0]["status"] == "failed"


def test_timeout_marks_the_run_failed(tmp_path) -> None:
    agent = FakeAgent(delay=0.2)
    scheduler = _scheduler(tmp_path, agent)
    task = scheduler.add_task("slow", "take forever", "interval", "30m", timeout=0)
    _make_due(scheduler, task.id)

    run(scheduler.run_due_tasks())

    stored = scheduler.get_task(task.id)
    assert stored is not None
    assert stored.status is TaskStatus.FAILED
    assert "Timed out" in stored.last_result
    assert stored.error_count == 1


def test_failed_task_backs_off_instead_of_hot_looping(tmp_path) -> None:
    agent = FakeAgent(error=RuntimeError("boom"))
    scheduler = _scheduler(tmp_path, agent)
    task = scheduler.add_task("broken", "do it", "interval", "1s")
    _make_due(scheduler, task.id)

    run(scheduler.run_due_tasks())

    stored = scheduler.get_task(task.id)
    assert stored is not None
    assert stored.status is TaskStatus.FAILED
    # a failure must not stay due, otherwise the daemon retries in a tight loop
    assert scheduler.get_due_tasks() == []
    assert datetime.fromisoformat(stored.next_run) > datetime.now()


def test_run_outliving_its_interval_is_pushed_forward(tmp_path) -> None:
    agent = FakeAgent(reply="done", delay=1.5)
    scheduler = _scheduler(tmp_path, agent)
    task = scheduler.add_task("slower than its interval", "do it", "interval", "1s")
    _make_due(scheduler, task.id)

    run(scheduler.run_due_tasks())

    stored = scheduler.get_task(task.id)
    assert stored is not None
    assert stored.status is TaskStatus.COMPLETED
    assert scheduler.get_due_tasks() == []


def test_persona_override_is_applied(tmp_path) -> None:
    agent = FakeAgent()
    scheduler = _scheduler(tmp_path, agent)
    task = scheduler.add_task("labelled", "do it", "interval", "30m", persona="coder")
    _make_due(scheduler, task.id)

    run(scheduler.run_due_tasks())

    assert agent.config.persona == "coder"


def test_remove_task_deletes_it_and_its_logs(tmp_path) -> None:
    agent = FakeAgent()
    scheduler = _scheduler(tmp_path, agent)
    task = scheduler.add_task("temporary", "do it", "interval", "30m")
    _make_due(scheduler, task.id)
    run(scheduler.run_due_tasks())

    assert scheduler.remove_task(task.id) is True
    assert scheduler.remove_task(task.id) is False
    assert scheduler.get_task(task.id) is None
    assert scheduler.get_logs(task.id) == []


def test_toggle_accepts_an_unknown_id(tmp_path) -> None:
    scheduler = _scheduler(tmp_path, FakeAgent())

    assert scheduler.toggle_task("no-such-id") is False


def test_toggle_flips_the_enabled_flag_when_no_value_is_given(tmp_path) -> None:
    scheduler = _scheduler(tmp_path, FakeAgent())
    task = scheduler.add_task("nightly", "do it", "interval", "30m")

    assert task.enabled is True
    assert scheduler.toggle_task(task.id) is True
    assert scheduler.get_task(task.id).enabled is False
    assert scheduler.toggle_task(task.id) is True
    assert scheduler.get_task(task.id).enabled is True


def test_a_corrupt_next_run_value_is_ignored_not_crashed(tmp_path) -> None:
    """A hand-edited or buggy next_run must not break due-task selection."""
    scheduler = _scheduler(tmp_path, FakeAgent())
    task = scheduler.add_task("nightly", "do it", "interval", "30m")
    stored = scheduler.get_task(task.id)
    assert stored is not None
    stored.next_run = "not-a-timestamp"

    assert scheduler.get_due_tasks() == []


def test_run_task_without_an_agent_factory_fails_the_run(tmp_path) -> None:
    scheduler = Scheduler(data_dir=tmp_path)
    task = scheduler.add_task("orphan", "do it", "interval", "30m")

    result = run(scheduler.run_task(scheduler.get_task(task.id)))

    assert result.startswith("Error: RuntimeError")
    stored = scheduler.get_task(task.id)
    assert stored is not None
    assert stored.status is TaskStatus.FAILED
    assert stored.error_count == 1
    assert scheduler.get_logs(task.id)[0]["status"] == "failed"


def test_background_loop_runs_due_tasks_and_stops(tmp_path) -> None:
    agent = FakeAgent(reply="background work done")
    scheduler = _scheduler(tmp_path, agent)
    task = scheduler.add_task("nightly", "summarise", "interval", "30m")
    _make_due(scheduler, task.id)

    async def _drive() -> None:
        loop_task = asyncio.create_task(
            scheduler.start_background_loop(check_interval=0)
        )
        await asyncio.sleep(0.05)  # let the loop pick the task up and run it
        scheduler.stop()
        await asyncio.wait_for(loop_task, timeout=2)

    run(_drive())

    assert agent.prompts == ["summarise"]
    stored = scheduler.get_task(task.id)
    assert stored is not None
    assert stored.run_count == 1


def test_background_loop_survives_a_failing_task(tmp_path) -> None:
    """An exception inside the loop body must not kill the daemon."""
    agent = FakeAgent(error=RuntimeError("exploded"))
    scheduler = _scheduler(tmp_path, agent)
    task = scheduler.add_task("nightly", "do it", "interval", "30m")
    _make_due(scheduler, task.id)

    async def _drive() -> None:
        loop_task = asyncio.create_task(
            scheduler.start_background_loop(check_interval=0)
        )
        await asyncio.sleep(0.05)
        scheduler.stop()
        await asyncio.wait_for(loop_task, timeout=2)

    run(_drive())  # must return instead of raising

    stored = scheduler.get_task(task.id)
    assert stored is not None
    assert stored.status is TaskStatus.FAILED


def test_background_loop_survives_due_selection_exploding(tmp_path, monkeypatch) -> None:
    """Even an error in get_due_tasks itself must not kill the loop."""
    scheduler = _scheduler(tmp_path, FakeAgent())
    calls = {"n": 0}
    original = scheduler.get_due_tasks

    def _flaky() -> list[ScheduledTask]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("sqlite is having a day")
        return original()

    monkeypatch.setattr(scheduler, "get_due_tasks", _flaky)

    async def _drive() -> None:
        loop_task = asyncio.create_task(
            scheduler.start_background_loop(check_interval=0)
        )
        await asyncio.sleep(0.05)
        scheduler.stop()
        await asyncio.wait_for(loop_task, timeout=2)

    run(_drive())  # first poll raised, second succeeded, loop lived on

    assert calls["n"] >= 2


def test_stop_is_idempotent(tmp_path) -> None:
    scheduler = _scheduler(tmp_path, FakeAgent())

    scheduler.stop()
    scheduler.stop()  # no raise

    assert scheduler._running is False
