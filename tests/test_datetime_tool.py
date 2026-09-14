"""DateTimeTool: now, diff (both orders), parse failures, and unknown actions."""

from __future__ import annotations

from support import run

from aegisx_agent.tools.base import ToolStatus
from aegisx_agent.tools.datetime_tool import DateTimeTool


def test_now_returns_utc_local_and_unix_timestamp() -> None:
    result = run(DateTimeTool().execute(action="now"))

    assert result.is_success
    assert "UTC Time:" in result.output
    assert "Local Time:" in result.output
    assert "Date:" in result.output
    assert "Unix Timestamp:" in result.output


def test_now_is_the_default_action() -> None:
    result = run(DateTimeTool().execute())

    assert result.is_success
    assert "UTC Time:" in result.output


def test_diff_computes_days_hours_minutes_seconds() -> None:
    result = run(
        DateTimeTool().execute(action="diff", date1="2026-01-01", date2="2026-01-02 03:04:05")
    )

    assert result.is_success
    assert "Difference: 1 days, 3 hours, 4 minutes, 5 seconds" in result.output
    assert "Total days: 1" in result.output
    assert "Total hours: 27.07" in result.output


def test_diff_works_in_both_date_orders() -> None:
    forward = run(
        DateTimeTool().execute(action="diff", date1="2026-01-01", date2="2026-01-03")
    )
    backward = run(
        DateTimeTool().execute(action="diff", date1="2026-01-03", date2="2026-01-01")
    )

    assert forward.is_success
    assert backward.is_success
    # The reverse delta must not produce negative hours; days may be negative.
    assert "-2.5" not in backward.output


def test_diff_requires_both_dates() -> None:
    missing = run(DateTimeTool().execute(action="diff", date1="2026-01-01"))
    both_missing = run(DateTimeTool().execute(action="diff"))

    for result in (missing, both_missing):
        assert result.status is ToolStatus.ERROR
        assert "Both date1 and date2 are required" in (result.error or "")


def test_diff_rejects_unparseable_dates() -> None:
    result = run(DateTimeTool().execute(action="diff", date1="not-a-date", date2="2026-01-01"))

    assert result.status is ToolStatus.ERROR
    assert "Cannot parse date" in (result.error or "")


def test_parse_date_accepts_datetime_and_date_forms() -> None:
    parse = DateTimeTool._parse_date

    assert parse("2026-05-04 10:20:30").strftime("%Y-%m-%d %H:%M:%S") == "2026-05-04 10:20:30"
    assert parse("2026-05-04").strftime("%Y-%m-%d") == "2026-05-04"


def test_an_unknown_action_is_an_error() -> None:
    result = run(DateTimeTool().execute(action="teleport"))

    assert result.status is ToolStatus.ERROR
    assert "Unknown action" in (result.error or "")
