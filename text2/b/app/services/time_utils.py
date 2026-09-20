"""Time helpers: timezone conversion and weekly-bucket splitting.

Weekly hours are counted against *each* ISO week the shift physically
touches. A shift crossing midnight in the worker's timezone contributes its
first portion to Monday–Sunday week N and the remainder to week N+1, so the
44-hour limit is enforced per week on actual worked minutes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


def get_zone(tz_name: str) -> ZoneInfo:
    return ZoneInfo(tz_name)


def to_utc(local_naive: datetime, tz_name: str) -> datetime:
    """Attach a plan/worker timezone and convert to aware UTC."""
    if local_naive.tzinfo is not None:
        local_naive = local_naive.replace(tzinfo=None)
    return local_naive.replace(tzinfo=get_zone(tz_name)).astimezone(UTC)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        # Naive values read back from SQLite have no tzinfo; treat as UTC.
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def local_date_of(moment_utc: datetime, tz_name: str) -> date:
    return moment_utc.astimezone(get_zone(tz_name)).date()


def combine_local(d: date, t: time, tz_name: str) -> datetime:
    """Build an aware UTC datetime from a local date and local clock time."""
    return datetime.combine(d, t).replace(tzinfo=get_zone(tz_name)).astimezone(UTC)


def iso_week_start(d: date) -> date:
    """Monday of the ISO week containing ``d``."""
    return d - timedelta(days=d.weekday())


@dataclass(frozen=True)
class WeekSlice:
    week_start: date  # Monday, in the given timezone
    duration: timedelta  # portion of the interval falling in that week


def split_interval_by_iso_week(
    starts_at: datetime, ends_at: datetime, tz_name: str
) -> list[WeekSlice]:
    """Split a UTC interval into slices keyed by local ISO-week Monday.

    Minutes are attributed to the week containing their local wall-clock day.
    """
    starts_at = ensure_utc(starts_at)
    ends_at = ensure_utc(ends_at)
    if ends_at <= starts_at:
        return []
    tz = get_zone(tz_name)
    cursor = starts_at
    slices: list[WeekSlice] = []
    while cursor < ends_at:
        local_cursor = cursor.astimezone(tz)
        week_monday = iso_week_start(local_cursor.date())
        # Next local Monday 00:00, converted back to UTC.
        next_monday_local = datetime.combine(
            week_monday + timedelta(days=7), time.min
        ).replace(tzinfo=tz)
        boundary_utc = next_monday_local.astimezone(UTC)
        slice_end = min(boundary_utc, ends_at)
        slices.append(WeekSlice(week_monday, slice_end - cursor))
        cursor = slice_end
    return slices


def iso_weeks_touched(
    starts_at: datetime, ends_at: datetime, tz_name: str
) -> set[date]:
    return {
        s.week_start
        for s in split_interval_by_iso_week(starts_at, ends_at, tz_name)
    }
