"""Unit tests for weekly-hour splitting and the 10h rest rule."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.timeutils import (
    REST_REQUIRED,
    iso_week_start,
    overlaps,
    split_into_weeks,
)


def test_iso_week_start_monday():
    # Wednesday -> Monday
    wed = datetime(2026, 9, 23, 14, tzinfo=timezone.utc)
    assert iso_week_start(wed) == datetime(2026, 9, 21, tzinfo=timezone.utc)
    sunday = datetime(2026, 9, 27, 23, 59, tzinfo=timezone.utc)
    assert iso_week_start(sunday) == datetime(2026, 9, 21, tzinfo=timezone.utc)
    monday = datetime(2026, 9, 28, 0, 0, tzinfo=timezone.utc)
    assert iso_week_start(monday) == monday


def test_overnight_shift_split_across_weeks():
    # Sunday 2026-09-27 22:00 -> Monday 2026-09-28 04:00 (6h, split 2h + 4h)
    start = datetime(2026, 9, 27, 22, tzinfo=timezone.utc)
    end = start + timedelta(hours=6)
    slices = split_into_weeks(start, end)
    assert len(slices) == 2
    assert slices[0].week_start == datetime(2026, 9, 21, tzinfo=timezone.utc)
    assert slices[0].duration == timedelta(hours=2)
    assert slices[1].week_start == datetime(2026, 9, 28, tzinfo=timezone.utc)
    assert slices[1].duration == timedelta(hours=4)


def test_split_long_shift_spans_multiple_weeks():
    # Sunday 12:00 + 30h = Tuesday 18:00: crosses one boundary (12h + 18h)
    start = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    end = start + timedelta(hours=30)
    slices = split_into_weeks(start, end)
    assert len(slices) == 2
    assert [s.duration for s in slices] == [
        timedelta(hours=12),
        timedelta(hours=18),
    ]
    assert sum((s.duration for s in slices), timedelta()) == timedelta(hours=30)


def test_overlap_half_open():
    t = datetime(2026, 9, 21, 8, tzinfo=timezone.utc)
    assert not overlaps(t, t + timedelta(hours=1), t + timedelta(hours=1), t + timedelta(hours=2))
    assert overlaps(t, t + timedelta(hours=2), t + timedelta(hours=1), t + timedelta(hours=3))


def test_rest_constant_is_ten_hours():
    assert REST_REQUIRED == timedelta(hours=10)
