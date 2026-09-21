package timeutil

import (
	"testing"
	"time"
)

func TestTruncateMinuteUTC(t *testing.T) {
	in, _ := time.Parse(time.RFC3339, "2026-03-10T09:00:45.123+02:00")
	got := TruncateMinuteUTC(in)
	want, _ := time.Parse(time.RFC3339, "2026-03-10T07:00:00Z")
	if !got.Equal(want) {
		t.Fatalf("got %s, want %s", got, want)
	}
}

func TestLocalDateAndWeek(t *testing.T) {
	la, _ := time.LoadLocation("America/Los_Angeles")
	// 2026-03-11 00:00 UTC = 2026-03-10 16:00 PDT.
	ts, _ := time.Parse(time.RFC3339, "2026-03-11T00:00:00Z")
	d := LocalDate(ts, la)
	want := time.Date(2026, 3, 10, 0, 0, 0, 0, time.UTC)
	if !d.Equal(want) {
		t.Fatalf("local date = %s, want 2026-03-10", d)
	}
	monday := MondayOfWeek(d)
	if monday.Format("2006-01-02") != "2026-03-09" {
		t.Fatalf("Monday = %s, want 2026-03-09", monday)
	}
}

func TestSundayBelongsToItsWeek(t *testing.T) {
	// Sunday 2026-03-08 should map to Monday 2026-03-02.
	d := time.Date(2026, 3, 8, 0, 0, 0, 0, time.UTC)
	if m := MondayOfWeek(d).Format("2006-01-02"); m != "2026-03-02" {
		t.Fatalf("Monday = %s", m)
	}
}
