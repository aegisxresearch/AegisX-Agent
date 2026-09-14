"""UsageTracker: transparent wrapping, JSONL recording, and summaries."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from support import run

from aegisx_agent.llm.base import LLMProvider, LLMResponse
from aegisx_agent.observability.usage import (
    USAGE_FILE,
    UsageTracker,
    parse_natural_time,
    read_usage,
)


class RecordingLLM(LLMProvider):
    """Provider stub emitting scripted usage dicts and recording calls."""

    def __init__(self) -> None:
        super().__init__(model="fake-model")
        self.responses: list[LLMResponse] = [
            LLMResponse(content="hi", usage={"prompt_tokens": 10, "completion_tokens": 5}),
            LLMResponse(
                content="ho",
                usage={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
            ),
            LLMResponse(content="done", usage={}),  # provider sent nothing
        ]

    async def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        return self.responses.pop(0)

    async def stream_chat(self, messages: Any, **kwargs: Any):  # type: ignore[override]
        response = self.responses.pop(0)
        yield "chunk"
        yield response


def _tracker(tmp_path: Path) -> tuple[UsageTracker, RecordingLLM]:
    inner = RecordingLLM()
    return UsageTracker(inner, tmp_path / USAGE_FILE), inner


def test_chat_records_usage_and_accumulates(tmp_path: Path) -> None:
    tracker, inner = _tracker(tmp_path)
    run(tracker.chat([{"role": "user", "content": "a"}]))
    run(tracker.chat([{"role": "user", "content": "b"}]))

    assert tracker.calls == 2
    assert tracker.input_tokens == 17  # 10 + 7 across both key spellings
    assert tracker.output_tokens == 8  # 5 + 3
    assert tracker.model == "fake-model"

    rows = read_usage(tracker.path)
    assert len(rows) == 2
    assert rows[0]["input_tokens"] == 10 and rows[0]["output_tokens"] == 5
    assert rows[1]["total_tokens"] == 10  # provider-provided total kept
    assert rows[0]["run_id"] == tracker.run_id


def test_streaming_records_only_the_final_response(tmp_path: Path) -> None:
    tracker, _ = _tracker(tmp_path)

    async def _collect() -> list[Any]:
        items = []
        async for item in tracker.stream_chat([{"role": "user", "content": "x"}]):
            items.append(item)
        return items

    items = run(_collect())
    assert items[0] == "chunk"
    assert isinstance(items[1], LLMResponse)
    assert tracker.calls == 1
    assert tracker.total_tokens == 10 + 5  # 10 in + 5 out from response 2
    assert len(read_usage(tracker.path)) == 1


def test_run_streaming_records_usage(tmp_path: Path) -> None:
    tracker, inner = _tracker(tmp_path)
    response = run(
        tracker.run_streaming([{"role": "user", "content": "x"}], on_chunk=None)
    )
    assert response.content
    assert tracker.calls == 1


def test_empty_usage_still_counts_a_call(tmp_path: Path) -> None:
    tracker, inner = _tracker(tmp_path)
    inner.responses = inner.responses[2:]  # keep only the empty-usage response
    run(tracker.chat([{"role": "user", "content": "z"}]))
    assert tracker.calls == 1
    assert tracker.total_tokens == 0
    row = read_usage(tracker.path)[0]
    assert row["total_tokens"] == 0


def test_summarize_filters_by_run_and_period(tmp_path: Path) -> None:
    tracker, _ = _tracker(tmp_path)
    for _ in range(2):
        run(tracker.chat([{"role": "user", "content": "a"}]))

    everything = tracker.summarize()
    assert everything["calls"] == 2

    only_this_run = tracker.summarize(run_id=tracker.run_id)
    assert only_this_run["calls"] == 2

    other = tracker.summarize(run_id="nope")
    assert other["calls"] == 0

    future = tracker.summarize(since="2999-01-01T00:00:00")
    assert future["calls"] == 0


def test_parse_natural_time_covers_documented_spellings() -> None:
    today = parse_natural_time("today")
    yesterday = parse_natural_time("yesterday")
    assert today is not None and yesterday is not None
    assert yesterday < today  # yesterday's cutoff precedes today's midnight
    assert parse_natural_time("7d") is not None
    assert parse_natural_time("24h") is not None
    assert parse_natural_time("bogus") is None


def test_read_usage_skips_corrupt_lines(tmp_path: Path) -> None:
    path = tmp_path / USAGE_FILE
    path.write_text(
        json.dumps({"calls": 1, "total_tokens": 5}) + "\nnot json\n\n",
        encoding="utf-8",
    )
    rows = read_usage(path)
    assert len(rows) == 1 and rows[0]["total_tokens"] == 5


def test_missing_usage_file_reads_as_empty(tmp_path: Path) -> None:
    assert read_usage(tmp_path / "absent.jsonl") == []
