"""Time helpers: local<->UTC conversion and cross-day / cross-week splitting.

Business rules expressed in local time (service windows, weekly caps) are
converted to aware UTC datetimes for storage and overlap math. Weekly capacity
is measured in the *worker's* home timezone, so a night shift crossing local
midnight is split between the two ISO weeks it touches.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


def get_zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def local_to_utc(local_date, local_time_value: time, zone: ZoneInfo) -> datetime:
    """Combine a local date + clock time into an aware UTC datetime."""
    naive = datetime.combine(local_date, local_time_value)
    return naive.replace(tzinfo=zone).astimezone(UTC)


def utc_in_zone(value: datetime, zone: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(zone)


def iso_week_key(value: datetime, zone: ZoneInfo) -> tuple[int, int]:
    """ISO (year, week) of ``value`` as seen in ``zone``.

    Local Monday 00:00 is the week boundary, so a shift starting Sunday 23:00
    UTC whose local time is already Monday counts toward the new week.
    """
    iso = utc_in_zone(value, zone).isocalendar()
    return iso[0], iso[1]


def iso_week_start_utc(year: int, week: int, zone: ZoneInfo) -> datetime:
    """UTC instant of local Monday 00:00 for the given ISO week."""
    thursday = datetime.fromisocalendar(year, week, 4).replace(tzinfo=UTC)
    monday = thursday - timedelta(days=3)
    local_midnight = monday.replace(tzinfo=zone)
    return local_midnight.astimezone(UTC)


@dataclass(frozen=True)
class WeekSlice:
    week: tuple[int, int]
    minutes: int


def split_minutes_by_week(
    start_utc: datetime, end_utc: datetime, zone: ZoneInfo
) -> list[WeekSlice]:
    """Split a UTC interval into ISO-week buckets using local week boundaries.

    Returns one ``WeekSlice`` per touched week with whole-minute durations.
    Crossing a DST transition is handled by wall-clock aware conversion.
    """
    if end_utc <= start_utc:
        return []

    start_week = iso_week_key(start_utc, zone)
    end_week = iso_week_key(end_utc, zone)
    if start_week == end_week:
        minutes = (end_utc - start_utc).total_seconds() / 60.0
        return [WeekSlice(start_week, round(minutes))]

    start_year, start_no = start_week
    boundary = iso_week_start_utc(end_week[0], end_week[1], zone)

    first_minutes = (boundary - start_utc).total_seconds() / 60.0
    second_minutes = (end_utc - boundary).total_seconds() / 60.0
    return [
        WeekSlice(start_week, round(first_minutes)),
        WeekSlice(end_week, round(second_minutes)),
    ]
