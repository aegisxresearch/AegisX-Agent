"""ScheduledTask: serialization round-trip and every ``calculate_next_run`` branch."""

from __future__ import annotations

from datetime import datetime, timedelta

from aegisx_agent.scheduler.task import ScheduledTask, ScheduleType, TaskStatus


def _task(schedule_type: ScheduleType, value: str) -> ScheduledTask:
    return ScheduledTask(
        id="t", name="t", prompt="p", schedule_type=schedule_type, schedule_value=value
    )


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #


def test_round_trip_preserves_every_field() -> None:
    created = "2026-09-13T10:00:00"
    original = ScheduledTask(
        id="abc",
        name="nightly",
        prompt="summarise",
        schedule_type=ScheduleType.WEEKLY,
        schedule_value="MON:09:00",
        enabled=False,
        status=TaskStatus.COMPLETED,
        created_at=created,
        last_run="2026-09-12T22:00:00",
        last_result="all quiet",
        next_run="2026-09-14T09:00:00",
        run_count=7,
        error_count=1,
        persona="coder",
        model="glm-5.3-flash",
        notify=False,
        timeout=45,
        metadata={"channel": "#ops"},
    )

    restored = ScheduledTask.from_dict(original.to_dict())

    assert restored == original
    assert restored.to_dict()["schedule_type"] == "weekly"
    assert restored.to_dict()["status"] == "completed"


def test_from_dict_defaults_and_drops_unknown_keys() -> None:
    restored = ScheduledTask.from_dict(
        {"id": "x", "name": "n", "prompt": "p", "bogus_key": "ignored"}
    )

    assert restored.schedule_type is ScheduleType.INTERVAL
    assert restored.status is TaskStatus.PENDING
    assert restored.enabled is True
    assert restored.metadata == {}


def test_from_dict_rejects_an_unknown_status() -> None:
    try:
        ScheduledTask.from_dict(
            {"id": "x", "name": "n", "prompt": "p", "status": "teleported"}
        )
    except ValueError:
        pass
    else:  # pragma: no cover - only reached when the guard disappears
        raise AssertionError("unknown status must raise ValueError")


def test_from_dict_rejects_an_unknown_schedule_type() -> None:
    try:
        ScheduledTask.from_dict(
            {"id": "x", "name": "n", "prompt": "p", "schedule_type": "hourly"}
        )
    except ValueError:
        pass
    else:  # pragma: no cover - only reached when the guard disappears
        raise AssertionError("unknown schedule_type must raise ValueError")


# --------------------------------------------------------------------------- #
# calculate_next_run: INTERVAL
# --------------------------------------------------------------------------- #


def test_interval_next_run_is_now_plus_value() -> None:
    now = datetime.now()
    nxt = datetime.fromisoformat(_task(ScheduleType.INTERVAL, "45m").calculate_next_run())

    assert timedelta(minutes=44) < nxt - now < timedelta(minutes=46)


def test_interval_with_an_unparseable_value_stays_empty() -> None:
    assert _task(ScheduleType.INTERVAL, "every so often").calculate_next_run() == ""


# --------------------------------------------------------------------------- #
# calculate_next_run: DAILY
# --------------------------------------------------------------------------- #


def test_daily_future_time_lands_today() -> None:
    """A target two hours out fires today — or tomorrow if we straddle midnight
    between building the target and parsing it (µs from the task's own clock)."""
    now = datetime.now()
    target = (now + timedelta(hours=2)).strftime("%H:%M")
    nxt = datetime.fromisoformat(_task(ScheduleType.DAILY, target).calculate_next_run())
    delta = nxt - now

    fired_today = timedelta(hours=1, minutes=59) < delta <= timedelta(hours=2, seconds=1)
    rolled = timedelta(hours=21, minutes=58) < delta < timedelta(hours=22, seconds=2)
    assert fired_today or rolled


def test_daily_past_time_rolls_to_tomorrow() -> None:
    now = datetime.now()
    target = (now - timedelta(hours=2)).strftime("%H:%M")
    nxt = datetime.fromisoformat(_task(ScheduleType.DAILY, target).calculate_next_run())

    assert timedelta(hours=20) < nxt - now < timedelta(hours=23)


def test_daily_bad_value_falls_back_to_empty() -> None:
    assert _task(ScheduleType.DAILY, "breakfast").calculate_next_run() == ""


# --------------------------------------------------------------------------- #
# calculate_next_run: WEEKLY
# --------------------------------------------------------------------------- #


def test_weekly_named_day_picks_the_next_occurrence() -> None:
    task = _task(ScheduleType.WEEKLY, "FRI:09:00")
    now = datetime.now()
    nxt = datetime.fromisoformat(task.calculate_next_run())

    assert nxt.weekday() == 4  # Friday
    assert nxt.hour == 9 and nxt.minute == 0
    assert nxt > now


def test_weekly_past_named_day_wraps_to_next_week() -> None:
    """A time already past today must not be returned in the past (regression)."""
    yesterday = (datetime.now() - timedelta(days=1)).weekday()
    day_name = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"][yesterday]
    task = _task(ScheduleType.WEEKLY, f"{day_name}:09:00")

    nxt = datetime.fromisoformat(task.calculate_next_run())

    assert nxt > datetime.now()
    assert nxt.weekday() == yesterday


def test_weekly_same_day_time_passed_rolls_exactly_one_week() -> None:
    """Target day = today, time just passed: next run is the same weekday +7 days."""
    now = datetime.now()
    today_name = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"][now.weekday()]
    past_time = (now - timedelta(minutes=30)).strftime("%H:%M")
    task = _task(ScheduleType.WEEKLY, f"{today_name}:{past_time}")

    nxt = datetime.fromisoformat(task.calculate_next_run())

    assert nxt > now
    assert nxt.weekday() == now.weekday()
    assert timedelta(days=6) < nxt - now < timedelta(days=8)


def test_weekly_bare_time_uses_today_as_the_anchor() -> None:
    """'HH:MM' without a day anchors on today — µs and midnight straddles aside."""
    now = datetime.now()
    future_time = (now + timedelta(hours=1)).strftime("%H:%M")
    task = _task(ScheduleType.WEEKLY, future_time)
    nxt = datetime.fromisoformat(task.calculate_next_run())
    delta = nxt - now

    fired_today = timedelta(0) < delta <= timedelta(hours=1, seconds=1)
    rolled = timedelta(hours=22, minutes=58) < delta < timedelta(hours=23, seconds=2)
    assert fired_today or rolled
    assert nxt.weekday() == now.weekday()


def test_weekly_bare_time_that_passed_rolls_a_week() -> None:
    now = datetime.now()
    past_time = (now - timedelta(hours=1)).strftime("%H:%M")
    task = _task(ScheduleType.WEEKLY, past_time)
    nxt = datetime.fromisoformat(task.calculate_next_run())

    assert nxt > now
    assert nxt.weekday() == now.weekday()
    assert timedelta(days=6) < nxt - now < timedelta(days=8)


def test_weekly_garbage_falls_back_to_empty() -> None:
    assert _task(ScheduleType.WEEKLY, "FRI:whatever").calculate_next_run() == ""


# --------------------------------------------------------------------------- #
# calculate_next_run: CRON
# --------------------------------------------------------------------------- #


def test_cron_every_n_minutes() -> None:
    now = datetime.now()
    nxt = datetime.fromisoformat(
        _task(ScheduleType.CRON, "*/20 * * * *").calculate_next_run()
    )

    assert timedelta(minutes=19) < nxt - now < timedelta(minutes=21)


def test_cron_every_n_hours() -> None:
    now = datetime.now()
    nxt = datetime.fromisoformat(_task(ScheduleType.CRON, "0 */3 * * *").calculate_next_run())

    assert timedelta(hours=2, minutes=59) < nxt - now < timedelta(hours=3, minutes=1)


def test_cron_specific_time_rolls_past_midnight() -> None:
    """'M H * * *' with a time already passed rolls to tomorrow."""
    now = datetime.now()
    target = now - timedelta(minutes=1)
    expr = f"{target.minute} {target.hour} * * *"
    nxt = datetime.fromisoformat(_task(ScheduleType.CRON, expr).calculate_next_run())

    assert timedelta(hours=23) < nxt - now < timedelta(hours=25)


def test_cron_specific_time_later_today() -> None:
    """'M H * * *' with a time later today fires today — unless we straddle
    midnight between computing the target and parsing it."""
    now = datetime.now()
    target = now + timedelta(minutes=10)
    expr = f"{target.minute} {target.hour} * * *"
    nxt = datetime.fromisoformat(_task(ScheduleType.CRON, expr).calculate_next_run())

    delta = nxt - now
    fired_today = timedelta(0) < delta <= timedelta(minutes=11)
    rolled_overnight = timedelta(hours=23, minutes=49) < delta < timedelta(hours=25)
    assert fired_today or rolled_overnight


def test_cron_too_few_fields_is_empty() -> None:
    assert _task(ScheduleType.CRON, "* * *").calculate_next_run() == ""


def test_cron_star_minute_star_hour_falls_through_to_empty() -> None:
    """'*' is neither */N nor a digit, so the parser has no answer."""
    assert _task(ScheduleType.CRON, "* * * * *").calculate_next_run() == ""


def test_cron_bad_interval_value_falls_through() -> None:
    assert _task(ScheduleType.CRON, "*/NaN * * * *").calculate_next_run() == ""
    assert _task(ScheduleType.CRON, "0 */NaN * * *").calculate_next_run() == ""


# --------------------------------------------------------------------------- #
# Status enum sanity
# --------------------------------------------------------------------------- #


def test_status_values_are_stable_api() -> None:
    assert {s.value for s in TaskStatus} == {
        "pending",
        "running",
        "completed",
        "failed",
        "paused",
    }
