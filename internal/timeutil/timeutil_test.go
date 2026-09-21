package timeutil

import (
	"testing"
	"time"
)

func TestWithinWindow(t *testing.T) {
	tests := []struct {
		name       string
		minute     int
		start, end int
		want       bool
	}{
		{"day window inside", 10*60 + 30, 8 * 60, 18 * 60, true},
		{"day window at start", 8 * 60, 8 * 60, 18 * 60, true},
		{"day window at end excluded", 18 * 60, 8 * 60, 18 * 60, false},
		{"day window before", 7*60 + 59, 8 * 60, 18 * 60, false},
		{"wrapping window late", 23 * 60, 22 * 60, 6 * 60, true},
		{"wrapping window early", 5*60 + 59, 22 * 60, 6 * 60, true},
		{"wrapping window at end excluded", 6 * 60, 22 * 60, 6 * 60, false},
		{"wrapping window at start", 22 * 60, 22 * 60, 6 * 60, true},
		{"wrapping window midday out", 12 * 60, 22 * 60, 6 * 60, false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := WithinWindow(tc.minute, tc.start, tc.end); got != tc.want {
				t.Errorf("WithinWindow(%d,%d,%d) = %v, want %v", tc.minute, tc.start, tc.end, got, tc.want)
			}
		})
	}
}

func TestMinuteOfDayAndDate(t *testing.T) {
	sh, _ := LoadZone("Asia/Shanghai")
	ny, _ := LoadZone("America/New_York")
	// 2026-09-15 04:30 UTC = 12:30 Shanghai, 00:30 New York (EDT, UTC-4)
	utc := time.Date(2026, 9, 15, 4, 30, 0, 0, time.UTC)

	if m := MinuteOfDay(utc, sh); m != 12*60+30 {
		t.Errorf("shanghai minute = %d", m)
	}
	if d := LocalDate(utc, sh); d != "2026-09-15" {
		t.Errorf("shanghai date = %s", d)
	}
	if m := MinuteOfDay(utc, ny); m != 30 {
		t.Errorf("newyork minute = %d", m)
	}
	if d := LocalDate(utc, ny); d != "2026-09-15" {
		t.Errorf("newyork date = %s", d)
	}
}

func TestWeekStart(t *testing.T) {
	sh, _ := LoadZone("Asia/Shanghai")
	ny, _ := LoadZone("America/New_York")

	// Sunday 2026-09-13 18:00 UTC = Mon 2026-09-14 02:00 in Shanghai, so the
	// local week starts Sep 14 even though the UTC instant is in the prior week.
	sunday := time.Date(2026, 9, 13, 18, 0, 0, 0, time.UTC)
	if ws := WeekStart(sunday, sh); ws != "2026-09-14" {
		t.Errorf("shanghai week start = %s, want 2026-09-14", ws)
	}
	// Same Sunday 04:00 UTC = Sunday Sep 13 00:00 EDT -> Monday Sep 7.
	sundayNY := time.Date(2026, 9, 13, 4, 0, 0, 0, time.UTC)
	if ws := WeekStart(sundayNY, ny); ws != "2026-09-07" {
		t.Errorf("newyork week start = %s, want 2026-09-07", ws)
	}
	// Monday 2026-09-14 14:00 UTC = Monday 10:00 EDT -> week Sep 14.
	mondayNY := time.Date(2026, 9, 14, 14, 0, 0, 0, time.UTC)
	if ws := WeekStart(mondayNY, ny); ws != "2026-09-14" {
		t.Errorf("newyork monday week start = %s, want 2026-09-14", ws)
	}
}
