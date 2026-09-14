"""Contract tests for the focused core domain API mixins."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from support import run

from aegisx_agent.core.memory_api import MemoryAPI
from aegisx_agent.core.rag_api import RAGAPI
from aegisx_agent.core.scheduler_api import SchedulerAPI


class RAGHarness(RAGAPI):
    """Minimal object exposing only the state required by ``RAGAPI``."""

    def __init__(self, engine) -> None:
        self._rag_engine = engine


class MemoryHarness(MemoryAPI):
    """Minimal object exposing only the stores required by ``MemoryAPI``."""

    def __init__(self) -> None:
        self.long_term = Mock()
        self.conversation = Mock()
        self.session_store = Mock()
        self.user_model = Mock()
        self.prompt_memory = Mock()


class SchedulerHarness(SchedulerAPI):
    """Minimal object exposing only the scheduler dependency."""

    def __init__(self, scheduler) -> None:
        self.scheduler = scheduler


def test_rag_api_delegates_ingest_and_search() -> None:
    engine = SimpleNamespace(
        ingest_file=AsyncMock(return_value=2),
        ingest_text=AsyncMock(return_value=3),
        search=AsyncMock(return_value=[{"content": "answer"}]),
    )
    api = RAGHarness(engine)

    assert run(api.ingest_document("guide.md")) == 2
    assert run(api.ingest_text("body", source="guide.md")) == 3
    assert run(api.search_knowledge("answer", top_k=5)) == [{"content": "answer"}]
    engine.ingest_file.assert_awaited_once_with("guide.md")
    engine.ingest_text.assert_awaited_once_with("body", source="guide.md")
    engine.search.assert_awaited_once_with("answer", top_k=5)


def test_rag_api_fails_closed_when_disabled() -> None:
    api = RAGHarness(None)

    with pytest.raises(RuntimeError, match="RAG is disabled"):
        run(api.ingest_text("body"))
    with pytest.raises(RuntimeError, match="RAG is disabled"):
        run(api.search_knowledge("query"))


def test_memory_api_delegates_facts_sessions_and_preferences() -> None:
    api = MemoryHarness()
    api.long_term.search.return_value = [{"category": "prefs", "content": "concise"}]
    api.session_store.search.return_value = [{"session_id": "s1"}]
    api.session_store.get_stats.return_value = {"total_messages": 1}

    api.remember("prefs", "concise")
    assert api.recall("concise") == [{"category": "prefs", "content": "concise"}]
    api.clear_memory()
    assert api.search_sessions("deploy") == [{"session_id": "s1"}]
    assert api.get_session_stats() == {"total_messages": 1}
    api.learn_preference("style", "brief")

    api.long_term.store.assert_called_once_with("prefs", "concise")
    api.conversation.clear.assert_called_once_with()
    api.user_model.learn_preference.assert_called_once_with("style", "brief")
    api.prompt_memory.add_user_info.assert_called_once_with("style: brief")


def test_scheduler_api_delegates_crud_and_execution() -> None:
    task = SimpleNamespace(to_dict=Mock(return_value={"id": "t1"}))
    scheduler = Mock()
    scheduler.add_task.return_value = task
    scheduler.list_tasks.return_value = [task]
    scheduler.get_task.return_value = task
    scheduler.remove_task.return_value = True
    scheduler.toggle_task.return_value = True
    scheduler.get_logs.return_value = [{"status": "completed"}]
    scheduler.run_task = AsyncMock(return_value="done")
    scheduler.run_due_tasks = AsyncMock(return_value=[{"task_id": "t1"}])
    scheduler.start_background_loop = AsyncMock()
    api = SchedulerHarness(scheduler)

    assert api.add_scheduled_task("daily", "run", "daily", "09:00", "coder", 30) == {"id": "t1"}
    assert api.remove_scheduled_task("t1") is True
    assert api.list_scheduled_tasks() == [{"id": "t1"}]
    assert api.toggle_scheduled_task("t1", False) is True
    assert api.get_scheduled_task_logs("t1", limit=4) == [{"status": "completed"}]
    assert run(api.run_scheduled_task_now("t1")) == "done"
    assert run(api.run_due_scheduled_tasks()) == [{"task_id": "t1"}]
    run(api.start_scheduler(check_interval=7))

    scheduler.add_task.assert_called_once_with(
        name="daily",
        prompt="run",
        schedule_type="daily",
        schedule_value="09:00",
        persona="coder",
        timeout=30,
    )
    scheduler.toggle_task.assert_called_once_with("t1", False)
    scheduler.get_logs.assert_called_once_with("t1", limit=4)
    scheduler.start_background_loop.assert_awaited_once_with(7)


def test_scheduler_api_reports_missing_task_without_running_it() -> None:
    scheduler = Mock()
    scheduler.get_task.return_value = None
    scheduler.run_task = AsyncMock()
    api = SchedulerHarness(scheduler)

    assert run(api.run_scheduled_task_now("missing")) == "Task missing not found"
    scheduler.run_task.assert_not_awaited()
