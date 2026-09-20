"""Week splitting: midnight-crossing shifts attributed to the right weeks."""
from __future__ import annotations

from datetime import datetime, timezone

from app.services import time_utils


def test_short_shift_stays_in_one_week():
    # Monday 09:00–10:00 UTC.
    start = datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)
    end = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)
    slices = time_utils.split_interval_by_iso_week(start, end, "UTC")
    assert len(slices) == 1
    assert slices[0].week_start == datetime(2026, 9, 21).date()
    assert int(slices[0].duration.total_seconds()) == 3600


def test_shift_crossing_midnight_splits_into_two_weeks():
    # Sunday 23:00 -> Monday 02:00 UTC: 60 min in week 1, 120 in week 2.
    start = datetime(2026, 9, 27, 23, 0, tzinfo=timezone.utc)
    end = datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)
    slices = time_utils.split_interval_by_iso_week(start, end, "UTC")
    assert len(slices) == 2
    week1 = datetime(2026, 9, 21).date()
    week2 = datetime(2026, 9, 28).date()
    by_week = {s.week_start: s.duration for s in slices}
    assert int(by_week[week1].total_seconds() // 60) == 60
    assert int(by_week[week2].total_seconds() // 60) == 120


def test_timezone_boundary_uses_local_wall_clock():
    # UTC Sunday 16:30 is already Monday 00:30 in Asia/Shanghai, so the whole
    # shift is attributed to the *next* local ISO week.
    start = datetime(2026, 9, 27, 16, 30, tzinfo=timezone.utc)
    end = datetime(2026, 9, 27, 17, 30, tzinfo=timezone.utc)
    slices = time_utils.split_interval_by_iso_week(start, end, "Asia/Shanghai")
    assert len(slices) == 1
    assert slices[0].week_start == datetime(2026, 9, 28).date()
