"""Time / calendar helpers.

Weekly bucketing uses **ISO weeks Monday 00:00 UTC**. The spec calls for a
44-hour weekly cap; an overnight/multi-day shift is split at the UTC week
boundary so its minutes count in both weeks. (The domain is China-centric,
where local and UTC week boundaries coincide; using UTC keeps the rule
unambiguous and is documented in the API docs.)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

WEEK_START_HOUR_UTC = 0  # ISO Monday 00:00 UTC
WEEK_CAP = timedelta(hours=44)
REST_REQUIRED = timedelta(hours=10)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso_week_start(value: datetime) -> datetime:
    """Monday 00:00 UTC of the ISO week containing ``value``."""
    value = as_utc(value)
    monday_date = (value - timedelta(days=value.weekday())).date()
    return datetime.combine(monday_date, time.min, tzinfo=timezone.utc)


def overlaps(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    """Half-open interval overlap test."""
    return start_a < end_b and start_b < end_a


@dataclass(frozen=True)
class WeekSlice:
    week_start: datetime
    duration: timedelta


def split_into_weeks(start: datetime, end: datetime) -> list[WeekSlice]:
    """Split an interval at ISO-week boundaries, returning minutes per week."""
    start, end = as_utc(start), as_utc(end)
    if end <= start:
        return []
    slices: list[WeekSlice] = []
    cursor = start
    while cursor < end:
        week_begin = iso_week_start(cursor)
        next_week = week_begin + timedelta(days=7)
        piece_end = min(end, next_week)
        slices.append(WeekSlice(week_begin, piece_end - cursor))
        cursor = piece_end
    return slices


def local_occurrence_interval(
    service_tz_name: str,
    day: date,
    window_start_minute: int,
    duration_minutes: int,
) -> tuple[datetime, datetime]:
    """Anchor an occurrence at the window start of a service-local day -> UTC interval."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(service_tz_name)
    local_start = datetime.combine(day, time.min, tzinfo=tz) + timedelta(
        minutes=window_start_minute
    )
    start = local_start.astimezone(timezone.utc)
    return start, start + timedelta(minutes=duration_minutes)


def service_local_date(value: datetime, service_tz_name: str) -> date:
    from zoneinfo import ZoneInfo

    return as_utc(value).astimezone(ZoneInfo(service_tz_name)).date()


def enumerate_service_dates(horizon_start: date, days: int) -> list[date]:
    return [horizon_start + timedelta(days=i) for i in range(days)]
