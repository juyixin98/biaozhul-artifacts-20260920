"""时间工具测试。"""
from __future__ import annotations

import pytest

from pitjoin.times import days, dt, hours, minutes, now_ms, parse_iso, to_iso


class TestTimeConversions:
    def test_dt_and_to_iso_roundtrip(self):
        ms = dt(2026, 6, 15, 12, 30, 45, 123)
        assert to_iso(ms) == "2026-06-15T12:30:45.123Z"

    def test_parse_iso_z_suffix(self):
        assert parse_iso("2026-01-01T00:00:00Z") == dt(2026, 1, 1)

    def test_parse_iso_with_offset(self):
        assert parse_iso("2026-01-01T02:00:00+02:00") == dt(2026, 1, 1)

    def test_parse_iso_naive_assumed_utc(self):
        assert parse_iso("2026-01-01T00:00:00") == dt(2026, 1, 1)

    def test_parse_iso_strips_whitespace(self):
        assert parse_iso("  2026-01-01T00:00:00Z  ") == dt(2026, 1, 1)

    def test_parse_iso_rejects_garbage(self):
        with pytest.raises(ValueError):
            parse_iso("not-a-time")

    def test_unit_helpers(self):
        assert minutes(1) == 60_000
        assert hours(1) == 3_600_000
        assert days(1) == 86_400_000
        assert hours(24) == days(1)

    def test_now_ms_is_positive_int(self):
        assert isinstance(now_ms(), int)
        assert now_ms() > 1_700_000_000_000
