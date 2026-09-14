"""Standard 5-field cron expression parsing.

Supported syntax (standard cron, no seconds):

- fields: minute hour day-of-month month day-of-week
- values: numbers, ``*``, ranges ``a-b``, steps ``*/n``, ``a-b/n``, ``a/n``,
  lists ``a,b,c``
- names: months (JAN-DEC) and weekdays (SUN-SAT), case-insensitive 3-letter
  abbreviations
- day-of-week: 0 and 7 both mean Sunday (cron convention)
- day-of-month / day-of-week: when BOTH are restricted, standard cron ORs
  them; a single restricted field is an AND with the rest.

``next_fire`` is exact: it walks the calendar with month/day/hour jumps, so
``*/15 * * * *`` lands on :00/:15/:30/:45 boundaries instead of drifting.
"""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta

_MONTH_NAMES = {abbr.lower(): number for number, abbr in enumerate(calendar.month_abbr) if abbr}
_WEEKDAY_NAMES = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}

_FIELD_RANGES: dict[str, tuple[int, int]] = {
    "minute": (0, 59),
    "hour": (0, 23),
    "day_of_month": (1, 31),
    "month": (1, 12),
    "day_of_week": (0, 7),
}


class CronError(ValueError):
    """Raised when a cron field cannot be parsed."""


def _parse_value(token: str, field_name: str) -> int:
    """Parse one numeric or named value within a field."""
    lowered = token.strip().lower()
    if field_name == "month" and lowered[:3] in _MONTH_NAMES:
        return _MONTH_NAMES[lowered[:3]]
    if field_name == "day_of_week" and lowered[:3] in _WEEKDAY_NAMES:
        return _WEEKDAY_NAMES[lowered[:3]]
    if not lowered.isdigit():
        raise CronError(f"invalid {field_name} value: {token!r}")
    number = int(lowered)
    low, high = _FIELD_RANGES[field_name]
    if not low <= number <= high:
        raise CronError(f"{field_name} value {number} out of range ({low}-{high})")
    return number


def _parse_field(field_expr: str, field_name: str) -> frozenset[int]:
    """Expand one cron field into the set of values it matches."""
    low, high = _FIELD_RANGES[field_name]
    values: set[int] = set()
    for part in field_expr.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"empty list item in {field_name}: {field_expr!r}")
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            if not step_text.isdigit() or int(step_text) < 1:
                raise CronError(f"invalid step in {field_name}: {field_expr!r}")
            step = int(step_text)
        if part == "*":
            values.update(range(low, high + 1, step))
        elif "-" in part.lstrip("-"):
            begin_text, end_text = part.split("-", 1)
            begin = _parse_value(begin_text, field_name)
            end = _parse_value(end_text, field_name)
            if begin > end:
                raise CronError(f"reversed range in {field_name}: {field_expr!r}")
            values.update(range(begin, end + 1, step))
        else:
            value = _parse_value(part, field_name)
            if step > 1:
                values.update(range(value, high + 1, step))
            else:
                values.add(value)
    if not values:
        raise CronError(f"{field_name} matches nothing: {field_expr!r}")
    return frozenset(values)


def _normalize_dow(values: frozenset[int]) -> frozenset[int]:
    """Cron treats 7 as Sunday too; fold it onto 0."""
    return frozenset(0 if value == 7 else value for value in values)


def _cron_dow(python_weekday: int) -> int:
    """Python Monday=0..Sunday=6 to cron Sunday=0..Saturday=6."""
    return (python_weekday + 1) % 7


def _day_matches(
    candidate: datetime,
    doms: frozenset[int],
    dows: frozenset[int],
    dom_restricted: bool,
    dow_restricted: bool,
) -> bool:
    """Apply the standard cron day rules to one candidate day."""
    if not dom_restricted and not dow_restricted:
        return True
    dom_hit = candidate.day in doms
    dow_hit = _cron_dow(candidate.weekday()) in dows
    if dom_restricted and dow_restricted:
        return dom_hit or dow_hit
    return dom_hit if dom_restricted else dow_hit


def _first_of_next_allowed_month(
    candidate: datetime, months: frozenset[int]
) -> datetime:
    """Midnight on day 1 of the next month that is in the month set."""
    year, month = candidate.year, candidate.month
    for _ in range(12):
        month += 1
        if month > 12:
            month, year = 1, year + 1
        if month in months:
            return candidate.replace(year=year, month=month, day=1, hour=0, minute=0)
    raise CronError("no allowed month")  # unreachable: months is never empty


def next_fire(expression: str, after: datetime) -> datetime | None:
    """Return the next matching datetime strictly after ``after``.

    Returns ``None`` for expressions that can never fire (February 30th,
    invalid fields) — callers decide whether that is an error or a fall-through.
    """
    fields = expression.split()
    if len(fields) != 5:
        return None
    try:
        minutes = _parse_field(fields[0], "minute")
        hours = _parse_field(fields[1], "hour")
        doms = _parse_field(fields[2], "day_of_month")
        months = _parse_field(fields[3], "month")
        dows = _normalize_dow(_parse_field(fields[4], "day_of_week"))
    except CronError:
        return None
    dom_restricted = fields[2] != "*"
    dow_restricted = fields[4] != "*"

    candidate = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    horizon = candidate.replace(year=candidate.year + 4)
    while candidate < horizon:
        if candidate.month not in months:
            candidate = _first_of_next_allowed_month(candidate, months)
            continue
        if not _day_matches(candidate, doms, dows, dom_restricted, dow_restricted):
            candidate = (candidate + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if candidate.hour not in hours:
            later = [hour for hour in hours if hour > candidate.hour]
            if later:
                candidate = candidate.replace(hour=min(later), minute=0)
            else:
                candidate = (candidate + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if candidate.minute in minutes:
            return candidate
        later_minutes = [minute for minute in minutes if minute > candidate.minute]
        if later_minutes:
            return candidate.replace(minute=min(later_minutes))
        candidate = candidate.replace(minute=0) + timedelta(hours=1)
    return None


__all__ = ["CronError", "next_fire"]
