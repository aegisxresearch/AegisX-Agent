"""Cron parser: standard semantics, exact boundaries, and impossible dates."""

from __future__ import annotations

from datetime import datetime

from aegisx_agent.scheduler.cron import next_fire


def test_every_n_minutes_lands_on_boundary() -> None:
    fired = next_fire("*/15 * * * *", datetime(2026, 9, 14, 10, 7, 30))

    assert fired == datetime(2026, 9, 14, 10, 15)


def test_every_n_hours_hits_next_allowed_hour() -> None:
    fired = next_fire("0 */3 * * *", datetime(2026, 9, 14, 10, 7, 30))

    assert fired == datetime(2026, 9, 14, 12, 0)


def test_specific_time_later_today() -> None:
    fired = next_fire("30 14 * * *", datetime(2026, 9, 14, 10, 0))

    assert fired == datetime(2026, 9, 14, 14, 30)


def test_specific_time_rolls_to_tomorrow() -> None:
    fired = next_fire("30 14 * * *", datetime(2026, 9, 14, 15, 0))

    assert fired == datetime(2026, 9, 15, 14, 30)


def test_weekday_name_picks_next_matching_day() -> None:
    # 2026-09-14 is a Monday; next MON is the 21st.
    fired = next_fire("0 9 * * MON", datetime(2026, 9, 14, 10, 0))

    assert fired == datetime(2026, 9, 21, 9, 0)


def test_weekday_range_skips_the_weekend() -> None:
    # Friday 2026-09-18 10:00 → next 1-5 morning is Monday the 21st.
    fired = next_fire("0 9 * * 1-5", datetime(2026, 9, 18, 10, 0))

    assert fired == datetime(2026, 9, 21, 9, 0)


def test_sunday_accepts_zero_and_seven() -> None:
    sunday_zero = next_fire("5 4 * * 0", datetime(2026, 9, 14, 10, 0))
    sunday_seven = next_fire("5 4 * * 7", datetime(2026, 9, 14, 10, 0))

    assert sunday_zero == sunday_seven == datetime(2026, 9, 20, 4, 5)


def test_dom_and_dow_both_restricted_are_ored() -> None:
    # Both restricted: fires on the 1st OR on Mondays.
    first = next_fire("0 0 1 * 1", datetime(2026, 9, 14, 10, 0))
    # Oct 1 2026 is a Thursday; the next Monday is Sep 21. OR means the
    # earlier candidate wins, so Monday Sep 21 fires before the 1st.
    assert first == datetime(2026, 9, 21, 0, 0)


def test_month_names_and_lists() -> None:
    fired = next_fire("0 0 1 JAN,MAR *", datetime(2026, 9, 14, 10, 0))

    assert fired == datetime(2027, 1, 1, 0, 0)


def test_step_within_range() -> None:
    # 10-40/15 -> minutes {10, 25, 40}; the next after 10:00 is 10:10.
    fired = next_fire("10-40/15 * * * *", datetime(2026, 9, 14, 10, 0))
    assert fired == datetime(2026, 9, 14, 10, 10)
    # ...and after 10:11 the next hit is 10:25.
    assert next_fire("10-40/15 * * * *", datetime(2026, 9, 14, 10, 11)) == datetime(
        2026, 9, 14, 10, 25
    )


def test_month_jump_is_exact() -> None:
    fired = next_fire("0 0 1 * *", datetime(2026, 9, 14, 10, 0))

    assert fired == datetime(2026, 10, 1, 0, 0)


def test_impossible_date_returns_none() -> None:
    assert next_fire("0 0 30 2 *", datetime(2026, 1, 1)) is None
    assert next_fire("0 0 31 4 *", datetime(2026, 1, 1)) is None  # April 31


def test_invalid_expressions_return_none() -> None:
    assert next_fire("@monthly", datetime(2026, 1, 1)) is None
    assert next_fire("* * *", datetime(2026, 1, 1)) is None
    assert next_fire("99 * * * *", datetime(2026, 1, 1)) is None
    assert next_fire("*/NaN * * * *", datetime(2026, 1, 1)) is None


def test_result_is_strictly_in_the_future() -> None:
    after = datetime(2026, 9, 14, 10, 15, 0)  # exactly on a boundary
    fired = next_fire("*/15 * * * *", after)

    assert fired is not None and fired > after
