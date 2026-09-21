package integration

import (
	"testing"

	"desklens/internal/testsupport"
)

// TestCrossDayAndWeek verifies that daily buckets use the employee-local date
// (even when the UTC and local day differ around midnight) and that weekly
// department buckets follow the local Monday..Sunday week, including a row
// whose UTC and local weeks differ.
func TestCrossDayAndWeek(t *testing.T) {
	e := setup(t)
	// Near-24h window so every UTC minute is inside the monitoring window.
	e.publishWidePolicy("00:00", "24:00")

	// Employee 102, Shanghai (UTC+8).
	// 2026-09-10 16:00 UTC = 2026-09-11 00:00 Shanghai -> local date Sep 11.
	s1 := utc(2026, 9, 10, 16, 0)
	// 2026-09-11 15:59 UTC = 2026-09-11 23:59 Shanghai -> same local date.
	s2 := utc(2026, 9, 11, 15, 59)
	// 2026-09-13 17:00 UTC = 2026-09-14 01:00 Shanghai (Mon) -> local week Sep 14,
	// even though the UTC instant is Sunday (UTC week of Sep 7).
	s3 := utc(2026, 9, 13, 17, 0)

	e.mustIngest(p102, "xd-1",
		snapAt("WS-102", 102, s1, "Chrome", 10))
	e.mustIngest(p102, "xd-2",
		snapAt("WS-102", 102, s2, "Chrome", 20))
	e.mustIngest(p102, "xd-3",
		snapAt("WS-102", 102, s3, "Zoom", 5))

	// s1 and s2 are the same Shanghai calendar date despite different UTC dates.
	d1, ok := testsupport.GetDaily(t, e.DB, 102, "2026-09-11")
	if !ok {
		t.Fatal("expected daily row for local 2026-09-11")
	}
	if d1.TotalCount != 30 {
		t.Fatalf("Sep 11 total = %d, want 30", d1.TotalCount)
	}
	if _, ok := testsupport.GetDaily(t, e.DB, 102, "2026-09-10"); ok {
		t.Fatal("UTC Sep 10 bucket should not exist; local date is Sep 11")
	}

	// s3 lands in local week Sep 14..20, even though its UTC instant is Sunday
	// (UTC week Sep 7).
	w, ok := testsupport.GetWeekly(t, e.DB, 1, "2026-09-14")
	if !ok {
		t.Fatal("expected weekly row for week 2026-09-14")
	}
	if w.TotalCount != 5 {
		t.Fatalf("week Sep 14 total = %d, want 5", w.TotalCount)
	}
	// The Friday Sep 11 rows stay in local week Sep 7 and must not be pulled
	// forward into Sep 14 by the UTC/zone mismatch.
	w, ok = testsupport.GetWeekly(t, e.DB, 1, "2026-09-07")
	if !ok {
		t.Fatal("expected weekly row for week 2026-09-07")
	}
	if w.TotalCount != 30 {
		t.Fatalf("week Sep 7 total = %d, want 30 (s1+s2 only)", w.TotalCount)
	}
}

// TestLateArrivals verifies that a snapshot arriving after its bucket's
// summaries already exist only recomputes the affected local date (and the
// enclosing week), and that totals remain exact.
func TestLateArrivals(t *testing.T) {
	e := setup(t)
	e.publishWidePolicy("00:00", "24:00")

	// Day 1: 3 snapshots for employee 101 in NY.
	e.mustIngest(p101, "late-1",
		snapAt("WS-101", 101, utc(2026, 9, 15, 14, 0), "Chrome", 10),
		snapAt("WS-101", 101, utc(2026, 9, 15, 14, 1), "Chrome", 20),
		snapAt("WS-101", 101, utc(2026, 9, 15, 14, 2), "WeChat", 3))

	dBefore, _ := testsupport.GetDaily(t, e.DB, 101, "2026-09-15")
	if dBefore.TotalCount != 33 {
		t.Fatalf("initial daily = %d, want 33", dBefore.TotalCount)
	}

	// A different day already finalized.
	e.mustIngest(p101, "late-2",
		snapAt("WS-101", 101, utc(2026, 9, 16, 14, 0), "Chrome", 7))
	dNeighbor, _ := testsupport.GetDaily(t, e.DB, 101, "2026-09-16")
	if dNeighbor.TotalCount != 7 {
		t.Fatalf("neighbor day = %d, want 7", dNeighbor.TotalCount)
	}

	// Late snapshot for Sep 15, minutes after the Sep 16 data exists.
	e.mustIngest(p101, "late-3",
		snapAt("WS-101", 101, utc(2026, 9, 15, 13, 30), "Chrome", 100))

	dAfter, _ := testsupport.GetDaily(t, e.DB, 101, "2026-09-15")
	if dAfter.TotalCount != 133 {
		t.Fatalf("late daily = %d, want 133", dAfter.TotalCount)
	}
	if dAfter.ProductiveCount != 130 {
		t.Fatalf("productive = %d, want 130", dAfter.ProductiveCount)
	}
	if dAfter.UnproductiveCount != 3 {
		t.Fatalf("unproductive = %d, want 3", dAfter.UnproductiveCount)
	}
	// Neighbor day untouched.
	if d, _ := testsupport.GetDaily(t, e.DB, 101, "2026-09-16"); d.TotalCount != 7 {
		t.Fatalf("neighbor day changed: %d", d.TotalCount)
	}
	// Both dates are the same NY week (Sep 14..20): the week reflects all.
	w, _ := testsupport.GetWeekly(t, e.DB, 1, "2026-09-14")
	if w.TotalCount != 140 {
		t.Fatalf("weekly total = %d, want 140", w.TotalCount)
	}
}
