package schedule

import (
	"testing"
	"time"

	"tztrig/internal/zoneinfo"
)

func mustLoc(t *testing.T, name string) *time.Location {
	t.Helper()
	loc, err := zoneinfo.Load(name)
	if err != nil {
		t.Fatalf("load zone %s: %v", name, err)
	}
	return loc
}

func utc(y int, mo time.Month, d, h, mi int) time.Time {
	return time.Date(y, mo, d, h, mi, 0, 0, time.UTC)
}

func TestEmbeddedDatabaseIsPinned(t *testing.T) {
	if zoneinfo.TZDataVersion != "2024a" {
		t.Fatalf("tests must run against pinned tzdata 2024a, got %q", zoneinfo.TZDataVersion)
	}
	// Alias from the backward file must be resolvable from the embedded tree.
	if _, err := zoneinfo.Load("US/Eastern"); err != nil {
		t.Fatalf("backward alias missing: %v", err)
	}
}

func TestParseFields(t *testing.T) {
	loc := mustLoc(t, "UTC")
	cases := []struct {
		minute, hour, day string
		wantErr           bool
	}{
		{"*", "*", "*", false},
		{"", "", "", false},
		{"0", "9", "1-5", false},
		{"*/15", "9-17", "1,2,3,4,5", false},
		{"0,30", "0,12", "0", false},
		{"30/15", "*", "*", false}, // 30,45 within the hour
		{"60", "*", "*", true},
		{"*", "24", "*", true},
		{"*", "*", "8", true},
		{"a", "*", "*", true},
		{"1-", "*", "*", true},
		{"5-2", "*", "*", true},
		{"*/0", "*", "*", true},
	}
	for _, c := range cases {
		s, err := Parse(c.minute, c.hour, c.day, loc)
		if c.wantErr {
			if err == nil {
				t.Errorf("Parse(%q,%q,%q) expected error, got %v", c.minute, c.hour, c.day, s.minuteOfDay)
			}
			continue
		}
		if err != nil {
			t.Errorf("Parse(%q,%q,%q) unexpected error: %v", c.minute, c.hour, c.day, err)
		}
	}

	// Sunday 7 must be normalized onto 0.
	s, _ := Parse("0", "0", "7", loc)
	if !s.weekdays.has(0) || s.weekdays.has(7) {
		t.Fatalf("weekday 7 not normalized to 0: %v", s.weekdays)
	}

	// "30/15" => minutes 30 and 45.
	s, _ = Parse("30/15", "*", "*", loc)
	var mods []int
	for _, m := range s.minuteOfDay {
		mods = append(mods, m%60)
	}
	if len(mods) != 24*2 || mods[0] != 30 || mods[1] != 45 {
		t.Fatalf("30/15 expansion wrong: %v", mods[:4])
	}
}

func TestBasicNext(t *testing.T) {
	loc := mustLoc(t, "UTC")
	s, err := Parse("*/30", "9-10", "1-5", loc) // weekdays 09:00/09:30/10:00/10:30
	if err != nil {
		t.Fatal(err)
	}
	// Saturday 2024-03-09 -> next fire Monday 09:00.
	after := utc(2024, 3, 9, 12, 0)
	got, ok := s.Next(after)
	if !ok {
		t.Fatal("no next fire found")
	}
	want := utc(2024, 3, 11, 9, 0)
	if !got.Equal(want) {
		t.Fatalf("got %s, want %s", got, want)
	}

	// Sequence of the four Monday morning firings.
	var seq []time.Time
	cur := after
	for i := 0; i < 4; i++ {
		n, ok := s.Next(cur)
		if !ok {
			t.Fatal("sequence ended early")
		}
		seq = append(seq, n)
		cur = n
	}
	wantSeq := []time.Time{
		utc(2024, 3, 11, 9, 0), utc(2024, 3, 11, 9, 30),
		utc(2024, 3, 11, 10, 0), utc(2024, 3, 11, 10, 30),
	}
	for i := range wantSeq {
		if !seq[i].Equal(wantSeq[i]) {
			t.Fatalf("seq[%d]=%s want %s", i, seq[i], wantSeq[i])
		}
	}
}

// TestSpringForwardGapSkipped verifies that a wall minute inside the
// 2024-03-10 02:00->03:00 America/New_York gap never fires.
func TestSpringForwardGapSkipped(t *testing.T) {
	ny := mustLoc(t, "America/New_York")
	s, err := Parse("30", "2", "*", ny) // daily 02:30 local
	if err != nil {
		t.Fatal(err)
	}
	// From 2024-03-09 local noon, the 03-10 02:30 does not exist; next is
	// 03-11 02:30 EDT = 06:30 UTC.
	after := time.Date(2024, 3, 9, 12, 0, 0, 0, ny)
	got, ok := s.Next(after)
	if !ok {
		t.Fatal("no next fire")
	}
	want := utc(2024, 3, 11, 6, 30)
	if !got.Equal(want) {
		t.Fatalf("gap minute not skipped: got %s want %s", got, want)
	}

	// Whole 02:xx hour on the gap day must vanish: enumerate fires for a
	// schedule matching every minute of hour 2 across the transition day.
	s2, _ := Parse("*", "2", "*", ny)
	var fires []time.Time
	s2.ForEachBetween(time.Date(2024, 3, 10, 1, 59, 0, 0, ny),
		time.Date(2024, 3, 10, 4, 0, 0, 0, ny), func(ft time.Time) bool {
			fires = append(fires, ft)
			return true
		})
	if len(fires) != 0 {
		t.Fatalf("expected no 02:xx fires on gap day, got %d: %v", len(fires), fires)
	}
}

// TestFallBackOverlapFiresOnce verifies the duplicated 01:30 wall minute on
// 2024-11-03 (EDT 05:30Z and EST 06:30Z) fires exactly once, at the earlier.
func TestFallBackOverlapFiresOnce(t *testing.T) {
	ny := mustLoc(t, "America/New_York")
	s, err := Parse("30", "1", "*", ny) // daily 01:30 local
	if err != nil {
		t.Fatal(err)
	}
	var fires []time.Time
	s.ForEachBetween(utc(2024, 11, 2, 0, 0), utc(2024, 11, 4, 0, 0),
		func(ft time.Time) bool {
			fires = append(fires, ft)
			return true
		})
	// Expect exactly two fires: 11-02 01:30 EDT and 11-03 01:30 EDT (earlier
	// of the two on the overlap day). The 06:30Z repetition must not fire.
	want := []time.Time{
		utc(2024, 11, 2, 5, 30),
		utc(2024, 11, 3, 5, 30),
	}
	if len(fires) != len(want) {
		t.Fatalf("got %d fires %v, want %d", len(fires), fires, len(want))
	}
	for i := range want {
		if !fires[i].Equal(want[i]) {
			t.Fatalf("fires[%d]=%s want %s", i, fires[i], want[i])
		}
	}
}

// TestSouthernHemisphereTransition checks Sydney, where DST runs the other
// way: spring gap 2024-10-06 02:00->03:00, fall overlap 2024-04-07.
func TestSouthernHemisphereTransition(t *testing.T) {
	syd := mustLoc(t, "Australia/Sydney")
	gap, err := Parse("15", "2", "*", syd) // 02:15 local
	if err != nil {
		t.Fatal(err)
	}
	got, ok := gap.Next(time.Date(2024, 10, 5, 12, 0, 0, 0, syd))
	if !ok {
		t.Fatal("no next fire")
	}
	if want := time.Date(2024, 10, 7, 2, 15, 0, 0, syd); !got.Equal(want) {
		t.Fatalf("Sydney gap: got %s want %s", got, want)
	}

	ov, err := Parse("30", "2", "*", syd) // on 2024-04-07, 02:30 occurs twice
	if err != nil {
		t.Fatal(err)
	}
	var fires []time.Time
	ov.ForEachBetween(utc(2024, 4, 5, 12, 0), utc(2024, 4, 7, 0, 0),
		func(ft time.Time) bool { fires = append(fires, ft); return true })
	// 04-06 02:30 AEDT = 04-05 15:30Z; on 04-07 the wall minute repeats
	// (15:30Z AEDT, 16:30Z AEST) and only the earlier one fires.
	want := []time.Time{utc(2024, 4, 5, 15, 30), utc(2024, 4, 6, 15, 30)}
	if len(fires) != 2 || !fires[0].Equal(want[0]) || !fires[1].Equal(want[1]) {
		t.Fatalf("Sydney overlap: got %v want %v", fires, want)
	}
}

func TestYearCrossing(t *testing.T) {
	ny := mustLoc(t, "America/New_York")
	s, err := Parse("0", "0", "1", ny) // Mondays 00:00, crosses years fine
	if err != nil {
		t.Fatal(err)
	}
	got, ok := s.Next(utc(2024, 12, 30, 12, 0))
	if !ok {
		t.Fatal("no next fire")
	}
	if want := utc(2025, 1, 6, 5, 0); !got.Equal(want) { // EST UTC-5
		t.Fatalf("year crossing: got %s want %s", got, want)
	}

	// New Year's Day midnight itself.
	daily, _ := Parse("0", "0", "*", ny)
	n, ok := daily.Next(time.Date(2024, 12, 31, 23, 59, 0, 0, ny))
	if !ok || !n.Equal(utc(2025, 1, 1, 5, 0)) {
		t.Fatalf("new year fire wrong: %v ok=%v", n, ok)
	}
}

func TestNextFromInsideGap(t *testing.T) {
	ny := mustLoc(t, "America/New_York")
	s, _ := Parse("*", "*", "*", ny) // every minute
	// Start at a real instant, 01:44 EST, and enumerate 20 minutes.
	anchor := time.Date(2024, 3, 10, 1, 44, 0, 0, ny)
	var fires []time.Time
	// 16 real minutes from 06:44Z lands at 07:00Z = 03:00 EDT.
	s.ForEachBetween(anchor, anchor.Add(16*time.Minute), func(ft time.Time) bool {
		fires = append(fires, ft)
		return true
	})
	// 01:45..01:59 EST (15) then the clock jumps to 03:00 EDT; no 02:xx.
	wantCount := 16
	if len(fires) != wantCount {
		t.Fatalf("got %d fires, want %d: %v", len(fires), wantCount, fires)
	}
	last := fires[len(fires)-1]
	if want := utc(2024, 3, 10, 7, 0); !last.Equal(want) {
		t.Fatalf("first post-gap fire = %s, want %s", last, want)
	}
	for _, f := range fires {
		if f.Hour() == 2 {
			t.Fatalf("gap minute fired: %s", f)
		}
	}
}
