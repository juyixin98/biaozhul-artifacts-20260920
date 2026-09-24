package engine

import (
	"strconv"
	"testing"
	"time"

	"tztrig/internal/zoneinfo"
)

func nyLoc(t *testing.T) *time.Location {
	t.Helper()
	loc, err := zoneinfo.Load("America/New_York")
	if err != nil {
		t.Fatal(err)
	}
	return loc
}

func newTestEngine(t *testing.T, clock Clock, catchUp int) *Engine {
	t.Helper()
	opts := []Option{}
	if catchUp > 0 {
		opts = append(opts, WithCatchUpLimit(catchUp))
	}
	return New(clock, opts...)
}

func enabled() *bool { b := true; return &b }

func TestAdvanceFiresAndDedupes(t *testing.T) {
	// Every minute UTC; create at 12:00:00.
	clk := NewFakeClock(utcT(2024, 3, 1, 12, 0))
	e := newTestEngine(t, clk, 100)
	if _, err := e.CreateSchedule(CreateInput{
		ID: "s1", Minute: "*", Hour: "*", Weekday: "*", Timezone: "UTC", Enabled: enabled(),
	}); err != nil {
		t.Fatal(err)
	}

	res := e.Advance(clk.Now())
	if len(res.Fired) != 0 {
		t.Fatalf("nothing should be due at the create instant, got %d", len(res.Fired))
	}

	clk.Set(utcT(2024, 3, 1, 12, 3))
	res = e.Advance(clk.Now())
	// Due at 12:01, 12:02, 12:03.
	if len(res.Fired) != 3 {
		t.Fatalf("want 3 fires, got %d (%v)", len(res.Fired), res.Fired)
	}

	// Re-advancing at the same instant must not re-fire (logical ID dedupe +
	// watermark).
	res2 := e.Advance(clk.Now())
	if len(res2.Fired) != 0 {
		t.Fatalf("re-advance fired %d duplicates", len(res2.Fired))
	}

	// IDs must be scheduleID:unix and globally unique.
	seen := map[string]bool{}
	for _, f := range res.Fired {
		if seen[f.ID] {
			t.Fatalf("duplicate fire id %s", f.ID)
		}
		seen[f.ID] = true
		if f.ID != "s1:"+strconv.FormatInt(f.EventTime.Unix(), 10) {
			t.Fatalf("fire id format wrong: %s", f.ID)
		}
	}
}

func TestCatchUpLimit(t *testing.T) {
	// Simulate long downtime: schedule created, then process "restarts"
	// 10,000 minutes later. Cap = 5.
	clk := NewFakeClock(utcT(2024, 1, 1, 0, 0))
	e := newTestEngine(t, clk, 5)
	if _, err := e.CreateSchedule(CreateInput{
		ID: "daily", Minute: "0", Hour: "0", Weekday: "*", Timezone: "UTC", Enabled: enabled(),
	}); err != nil {
		t.Fatal(err)
	}
	// Jump ~30 days forward: 30 missed midnights, only the latest 5 fire.
	clk.Set(utcT(2024, 1, 31, 0, 0))
	res := e.Advance(clk.Now())
	if len(res.Fired) != 5 {
		t.Fatalf("want 5 capped catch-up fires, got %d", len(res.Fired))
	}
	if len(res.Skipped) != 1 || res.Skipped[0].Count != 25 {
		t.Fatalf("want 25 skipped, got %+v", res.Skipped)
	}
	// Delivered firings must be the 5 most recent, and marked catch-up.
	// Due window is strictly after the Jan 1 00:00 creation: Jan 2..Jan 31.
	wantFirst := utcT(2024, 1, 27, 0, 0)
	if !res.Fired[0].EventTime.Equal(wantFirst) {
		t.Fatalf("first delivered = %s, want %s", res.Fired[0].EventTime, wantFirst)
	}
	for _, f := range res.Fired {
		// The boundary fire exactly at restart time is "due"; earlier ones
		// were missed and must be catch-up.
		wantReason := ReasonCatchUp
		if f.EventTime.Equal(clk.Now()) {
			wantReason = ReasonDue
		}
		if f.Reason != wantReason {
			t.Fatalf("fire %s reason=%s want %s", f.EventTime, f.Reason, wantReason)
		}
	}
	// Skipped range must identify the dropped window.
	if !res.Skipped[0].Oldest.Equal(utcT(2024, 1, 2, 0, 0)) {
		t.Fatalf("skipped oldest wrong: %s", res.Skipped[0].Oldest)
	}
	if !res.Skipped[0].Newest.Equal(utcT(2024, 1, 26, 0, 0)) {
		t.Fatalf("skipped newest wrong: %s", res.Skipped[0].Newest)
	}

	// Advancing again must not replay the dropped backlog.
	res2 := e.Advance(clk.Now())
	if len(res2.Fired) != 0 || len(res2.Skipped) != 0 {
		t.Fatalf("dropped backlog replayed: %+v", res2)
	}
}

func TestCatchUpAcrossYearEnd(t *testing.T) {
	clk := NewFakeClock(utcT(2024, 12, 31, 23, 0))
	e := newTestEngine(t, clk, 10)
	if _, err := e.CreateSchedule(CreateInput{
		ID: "hourly", Minute: "0", Hour: "*", Weekday: "*", Timezone: "UTC", Enabled: enabled(),
	}); err != nil {
		t.Fatal(err)
	}
	// Downtime spanning the year boundary.
	clk.Set(utcT(2025, 1, 1, 2, 0))
	res := e.Advance(clk.Now())
	want := []time.Time{
		utcT(2025, 1, 1, 0, 0),
		utcT(2025, 1, 1, 1, 0),
		utcT(2025, 1, 1, 2, 0),
	}
	if len(res.Fired) != len(want) {
		t.Fatalf("got %d fires: %v", len(res.Fired), res.Fired)
	}
	for i, w := range want {
		if !res.Fired[i].EventTime.Equal(w) {
			t.Fatalf("fire[%d]=%s want %s", i, res.Fired[i].EventTime, w)
		}
	}
}

func TestSpringForwardAcrossDowntime(t *testing.T) {
	// Schedule 02:30 daily NY. Created Mar 9, restarted Mar 11 at noon.
	// The Mar 10 02:30 does not exist (gap) and must never appear; the
	// delivered catch-up is only Mar 11 02:30 EDT.
	clk := NewFakeClock(nyTime(t, 2024, 3, 9, 12, 0))
	e := newTestEngine(t, clk, 50)
	if _, err := e.CreateSchedule(CreateInput{
		ID: "g", Minute: "30", Hour: "2", Weekday: "*", Timezone: "America/New_York", Enabled: enabled(),
	}); err != nil {
		t.Fatal(err)
	}
	clk.Set(nyTime(t, 2024, 3, 11, 12, 0))
	res := e.Advance(clk.Now())
	if len(res.Fired) != 1 {
		t.Fatalf("want exactly 1 fire (gap day skipped), got %d: %v", len(res.Fired), res.Fired)
	}
	if want := utcT(2024, 3, 11, 6, 30); !res.Fired[0].EventTime.Equal(want) {
		t.Fatalf("fire = %s want %s", res.Fired[0].EventTime, want)
	}
	if len(res.Skipped) != 0 {
		t.Fatalf("gap minutes must be skipped silently, not reported as overflow: %+v", res.Skipped)
	}
}

func TestFallBackAcrossDowntimeFiresOnce(t *testing.T) {
	clk := NewFakeClock(nyTime(t, 2024, 11, 2, 12, 0))
	e := newTestEngine(t, clk, 50)
	if _, err := e.CreateSchedule(CreateInput{
		ID: "o", Minute: "30", Hour: "1", Weekday: "*", Timezone: "America/New_York", Enabled: enabled(),
	}); err != nil {
		t.Fatal(err)
	}
	// Restart Nov 4: would-be firings are Nov 3 01:30 (twice on the wall,
	// but one logical trigger) and Nov 4 01:30.
	clk.Set(nyTime(t, 2024, 11, 4, 12, 0))
	res := e.Advance(clk.Now())
	if len(res.Fired) != 2 {
		t.Fatalf("want 2 fires, got %d: %v", len(res.Fired), res.Fired)
	}
	if want := utcT(2024, 11, 3, 5, 30); !res.Fired[0].EventTime.Equal(want) {
		t.Fatalf("overlap fire must be the earlier 05:30Z, got %s", res.Fired[0].EventTime)
	}
	if want := utcT(2024, 11, 4, 6, 30); !res.Fired[1].EventTime.Equal(want) {
		t.Fatalf("second fire wrong: %s", res.Fired[1].EventTime)
	}
	// Logical IDs on the overlap day must be unique (earlier instant only).
	if res.Fired[0].ID == res.Fired[1].ID {
		t.Fatal("overlap produced colliding logical IDs")
	}
}

func TestPauseResume(t *testing.T) {
	clk := NewFakeClock(utcT(2024, 3, 1, 12, 0))
	e := newTestEngine(t, clk, 100)
	_, _ = e.CreateSchedule(CreateInput{
		ID: "p", Minute: "*", Hour: "*", Weekday: "*", Timezone: "UTC", Enabled: enabled(),
	})
	if err := e.SetEnabled("p", false); err != nil {
		t.Fatal(err)
	}
	clk.Set(utcT(2024, 3, 1, 13, 0))
	if res := e.Advance(clk.Now()); len(res.Fired) != 0 {
		t.Fatalf("paused schedule fired: %v", res.Fired)
	}
	if err := e.SetEnabled("p", true); err != nil {
		t.Fatal(err)
	}
	// While paused no watermark moved; resuming must catch up everything due.
	res := e.Advance(clk.Now())
	if len(res.Fired) != 60 {
		t.Fatalf("want 60 catch-up fires after resume, got %d", len(res.Fired))
	}
}

func TestValidationErrors(t *testing.T) {
	e := newTestEngine(t, NewFakeClock(utcT(2024, 1, 1, 0, 0)), 10)
	cases := []CreateInput{
		{ID: "", Timezone: "UTC"},
		{ID: "x", Timezone: "Mars/Olympus"},
		{ID: "y", Minute: "99", Timezone: "UTC"},
	}
	for _, in := range cases {
		if _, err := e.CreateSchedule(in); err == nil {
			t.Fatalf("expected error for %+v", in)
		}
	}
	if err := e.DeleteSchedule("ghost"); err != ErrNotFound {
		t.Fatalf("want ErrNotFound, got %v", err)
	}
}

func TestNextWakeup(t *testing.T) {
	clk := NewFakeClock(utcT(2024, 3, 1, 12, 0))
	e := newTestEngine(t, clk, 10)
	_, _ = e.CreateSchedule(CreateInput{
		ID: "n", Minute: "15", Hour: "18", Weekday: "*", Timezone: "UTC", Enabled: enabled(),
	})
	next, id, ok := e.NextWakeup()
	if !ok || id != "n" {
		t.Fatalf("NextWakeup ok=%v id=%q", ok, id)
	}
	if want := utcT(2024, 3, 1, 18, 15); !next.Equal(want) {
		t.Fatalf("next wakeup = %s want %s", next, want)
	}
}

// ---- helpers ----

func utcT(y int, mo time.Month, d, h, mi int) time.Time {
	return time.Date(y, mo, d, h, mi, 0, 0, time.UTC)
}

func nyTime(t *testing.T, y int, mo time.Month, d, h, mi int) time.Time {
	t.Helper()
	return time.Date(y, mo, d, h, mi, 0, 0, nyLoc(t))
}
