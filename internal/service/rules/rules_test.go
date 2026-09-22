package rules

import (
	"testing"
	"time"
)

func TestContainingWindowsHalfOpenMembership(t *testing.T) {
	loc := time.UTC
	w := 5 * time.Minute
	// An event at 10:04:30 is contained in windows starting 10:00..10:04.
	tAt := time.Date(2026, 1, 1, 10, 4, 30, 0, loc)
	ws := ContainingWindows(tAt, w, loc)
	if len(ws) != 5 {
		t.Fatalf("got %d windows, want 5", len(ws))
	}
	wantStarts := []string{"10:04", "10:03", "10:02", "10:01", "10:00"}
	for i, want := range wantStarts {
		if ws[i].Format("15:04") != want {
			t.Fatalf("window[%d] start = %s, want %s", i, ws[i].Format("15:04"), want)
		}
	}

	// Half-open check: event exactly at a window END must not be in that
	// window's [start,end) range. tAt' = 10:05:00 belongs to [10:01,10:06)?
	// No: 10:05:00 is >= end of [10:00,10:05), so that window drops out and
	// [10:05,...] joins.
	tEdge := time.Date(2026, 1, 1, 10, 5, 0, 0, loc)
	es := ContainingWindows(tEdge, w, loc)
	start1000 := time.Date(2026, 1, 1, 10, 0, 0, 0, loc)
	for _, s := range es {
		if s.Equal(start1000) {
			t.Fatalf("10:00 window must be half-open; event at 10:05 excluded")
		}
	}
	// and the newest window starts at 10:05, containing the boundary event.
	if !es[0].Equal(start1000.Add(5 * time.Minute)) {
		t.Fatalf("newest window start = %s, want 10:05", es[0].Format("15:04"))
	}
}

func TestSensitiveAllowedHalfOpen(t *testing.T) {
	p := SensitiveHoursParams{AllowedStartHour: 6, AllowedEndHour: 20}
	loc := time.UTC
	at := func(hh int) time.Time { return time.Date(2026, 1, 1, hh, 0, 0, 0, loc) }
	if !p.Allowed(at(6)) {
		t.Fatal("06:00 must be allowed (inclusive start)")
	}
	if p.Allowed(at(20)) {
		t.Fatal("20:00 must be forbidden (exclusive end)")
	}
	if p.Allowed(at(5)) || p.Allowed(at(23)) {
		t.Fatal("outside hours must be forbidden")
	}
}

func TestSensitiveAllowedOvernight(t *testing.T) {
	p := SensitiveHoursParams{AllowedStartHour: 22, AllowedEndHour: 6}
	loc := time.UTC
	at := func(hh int) time.Time { return time.Date(2026, 1, 1, hh, 0, 0, 0, loc) }
	if !p.Allowed(at(23)) || !p.Allowed(at(2)) {
		t.Fatal("overnight window should contain 23:00 and 02:00")
	}
	if p.Allowed(at(6)) || p.Allowed(at(21)) {
		t.Fatal("06:00 (exclusive) and 21:00 must be forbidden")
	}
}
