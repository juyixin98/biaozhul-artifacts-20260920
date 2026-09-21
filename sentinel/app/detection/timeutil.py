from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Events that count toward file-access baselines.
BASELINE_EVENT_TYPES = ("file_access", "file_download")


def user_timezone(tz_name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def local_time(when_utc: datetime, tz_name: str | None) -> datetime:
    if when_utc.tzinfo is None:
        when_utc = when_utc.replace(tzinfo=timezone.utc)
    return when_utc.astimezone(user_timezone(tz_name))


def is_outside_work_hours(when_utc: datetime, tz_name: str | None, start_hour: int, end_hour: int) -> bool:
    """Outside [start_hour, end_hour) in the employee's organization timezone.

    22:00 is outside, 06:00 is inside (half-open interval).
    """
    local = local_time(when_utc, tz_name)
    hour = local.hour
    return not (start_hour <= hour < end_hour)
