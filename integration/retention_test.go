package integration

import (
	"context"
	"testing"

	"desklens/internal/admin"
	"desklens/internal/model"
	"desklens/internal/testsupport"
)

// TestPurgeBoundary verifies:
//   - purging raw data keeps summaries intact and records the rebuild boundary
//   - a rebuild touching purged history is refused: partial history must never
//     overwrite complete statistics
//   - ingesting a snapshot older than the boundary is refused
//   - the boundary only moves forward
//   - retention status reports the reconstructable range
func TestPurgeBoundary(t *testing.T) {
	e := setup(t) // v1 window 08:00-18:00 NY

	// Data in three separate NY local weeks (all at 10:00 EDT = 14:00 UTC).
	e.mustIngest(p101, "purge-w1",
		snapAt("WS-101", 101, utc(2026, 9, 2, 14, 0), "Chrome", 100))
	e.mustIngest(p101, "purge-w2",
		snapAt("WS-101", 101, utc(2026, 9, 9, 14, 0), "Chrome", 200))
	e.mustIngest(p101, "purge-w3",
		snapAt("WS-101", 101, utc(2026, 9, 16, 14, 0), "Chrome", 300))

	w1, ok := testsupport.GetWeekly(t, e.DB, 1, "2026-08-31")
	if !ok || w1.TotalCount != 100 {
		t.Fatalf("week1 summary = %+v ok=%v", w1, ok)
	}
	w2, _ := testsupport.GetWeekly(t, e.DB, 1, "2026-09-07")
	if w2.TotalCount != 200 {
		t.Fatalf("week2 = %d", w2.TotalCount)
	}
	w3, _ := testsupport.GetWeekly(t, e.DB, 1, "2026-09-14")
	if w3.TotalCount != 300 {
		t.Fatalf("week3 = %d", w3.TotalCount)
	}

	// Purge raw rows strictly before 2026-09-16T00:00Z. Weeks 1 and 2 raw data
	// goes; week 3 (Sep 16 14:00Z) survives.
	out, err := e.Admin.Purge(context.Background(), adminPurge("2026-09-16T00:00:00Z"))
	if err != nil {
		t.Fatal(err)
	}
	if out.DeletedRows != 2 {
		t.Fatalf("deleted = %d, want 2", out.DeletedRows)
	}
	if out.EarliestSnapshot == "" ||
		out.EarliestSnapshot != "2026-09-16T14:00:00Z" {
		t.Fatalf("earliest = %q, want 2026-09-16T14:00:00Z", out.EarliestSnapshot)
	}

	// Summaries survive untouched and are marked frozen where their raw history
	// was purged.
	w1, ok = testsupport.GetWeekly(t, e.DB, 1, "2026-08-31")
	if !ok || w1.TotalCount != 100 || !w1.Frozen {
		t.Fatalf("purged week1 summary lost/changed/not frozen: %+v", w1)
	}
	w2, _ = testsupport.GetWeekly(t, e.DB, 1, "2026-09-07")
	if w2.TotalCount != 200 || !w2.Frozen {
		t.Fatalf("purged week2 changed/not frozen: %+v", w2)
	}
	if d, ok := testsupport.GetDaily(t, e.DB, 101, "2026-09-02"); !ok || d.TotalCount != 100 || !d.Frozen {
		t.Fatalf("purged daily lost/not frozen: %+v", d)
	}
	// Surviving data stays rebuildable and is not frozen.
	if d, ok := testsupport.GetDaily(t, e.DB, 101, "2026-09-16"); !ok || d.Frozen {
		t.Fatalf("surviving daily wrongly frozen: %+v", d)
	}
	if w3.Frozen {
		t.Fatal("surviving week should not be frozen")
	}

	// A rebuild range that includes a frozen existing daily/weekly row is
	// refused. Sep 9 is frozen and lies in the frozen Sep 7 week.
	if _, err := e.Admin.Rebuild(context.Background(),
		admin.RebuildInput{StartDate: "2026-09-09", EndDate: "2026-09-16"}); err == nil {
		t.Fatal("expected rebuild_range_purged, got success")
	} else if ae, ok := err.(*admin.APIError); !ok || ae.Code != "rebuild_range_purged" {
		t.Fatalf("error = %v", err)
	}

	// The purged summaries survive the refused rebuild attempts.
	w1, _ = testsupport.GetWeekly(t, e.DB, 1, "2026-08-31")
	if w1.TotalCount != 100 {
		t.Fatalf("week1 overwritten after refused rebuild: %d", w1.TotalCount)
	}
	w2, _ = testsupport.GetWeekly(t, e.DB, 1, "2026-09-07")
	if w2.TotalCount != 200 {
		t.Fatalf("week2 overwritten after refused rebuild: %d", w2.TotalCount)
	}

	// Rebuild on a fully preserved date/week is allowed (complete raw history).
	e.mustRebuild("2026-09-16", "2026-09-16")
	w3, _ = testsupport.GetWeekly(t, e.DB, 1, "2026-09-14")
	if w3.TotalCount != 300 || w3.Frozen {
		t.Fatalf("safe rebuild changed surviving week: %+v", w3)
	}
	w1, _ = testsupport.GetWeekly(t, e.DB, 1, "2026-08-31")
	if w1.TotalCount != 100 {
		t.Fatalf("week1 changed by safe rebuild: %d", w1.TotalCount)
	}

	// Ingest older than the boundary is refused (cannot create unrebuildable
	// rows behind the retention marker).
	e.ingestErr(p101, []model.Snapshot{
		snapAt("WS-101", 101, utc(2026, 9, 1, 14, 0), "Chrome", 1),
	}, "beyond_retention_boundary")

	// Ingest at/after the boundary still works; its week has complete raw
	// history so the weekly total advances to 350 (300 + 50).
	e.mustIngest(p101, "after-boundary",
		snapAt("WS-101", 101, utc(2026, 9, 17, 14, 0), "Chrome", 50))
	if d, ok := testsupport.GetDaily(t, e.DB, 101, "2026-09-17"); !ok || d.TotalCount != 50 {
		t.Fatalf("post-boundary day = %+v", d)
	}
	w3, _ = testsupport.GetWeekly(t, e.DB, 1, "2026-09-14")
	if w3.TotalCount != 350 || w3.Frozen {
		t.Fatalf("surviving week after new ingest = %+v", w3)
	}

	// Boundary cannot move backward.
	if _, err := e.Admin.Purge(context.Background(), adminPurge("2026-09-10T00:00:00Z")); err == nil {
		t.Fatal("backward purge accepted")
	} else if ae, ok := err.(*admin.APIError); !ok || ae.Code != "cutoff_before_boundary" {
		t.Fatalf("backward purge error = %v", err)
	}

	// Retention status reports the marker.
	st, err := e.Admin.RetentionStatus(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if !st.HasPurged || st.EarliestSnapshot != "2026-09-16T14:00:00Z" {
		t.Fatalf("status = %+v", st)
	}
	if len(st.Events) != 1 || st.Events[0].DeletedRows != 2 {
		t.Fatalf("cleanup events = %+v", st.Events)
	}
}

func adminPurge(cutoff string) admin.PurgeInput {
	return admin.PurgeInput{Cutoff: cutoff, TriggeredBy: "test"}
}

// TestPurgeKeepsPartialRecomputeExact verifies a late row after a purge
// recomputes only surviving dates and leaves purged summaries untouched.
func TestPurgeLateRowSafety(t *testing.T) {
	e := setup(t)
	e.publishWidePolicy("00:00", "24:00")
	// Sep 15 (Tue) and Sep 17 (Thu) are in the SAME NY local week Sep 14.
	e.mustIngest(p101, "pl-1",
		snapAt("WS-101", 101, utc(2026, 9, 15, 14, 0), "Chrome", 100))
	e.mustIngest(p101, "pl-2",
		snapAt("WS-101", 101, utc(2026, 9, 17, 14, 0), "Chrome", 5))

	if _, err := e.Admin.Purge(context.Background(),
		adminPurge("2026-09-16T00:00:00Z")); err != nil {
		t.Fatal(err)
	}
	// The week contains purged Tuesday raw rows, so its complete total (105) is
	// frozen; the Tuesday daily row is frozen as well.
	w, ok := testsupport.GetWeekly(t, e.DB, 1, "2026-09-14")
	if !ok || !w.Frozen {
		t.Fatalf("week should be frozen: %+v ok=%v", w, ok)
	}
	if d, _ := testsupport.GetDaily(t, e.DB, 101, "2026-09-15"); d.TotalCount != 100 || !d.Frozen {
		t.Fatalf("purged tuesday = %+v", d)
	}
	if d, _ := testsupport.GetDaily(t, e.DB, 101, "2026-09-17"); d.Frozen {
		t.Fatal("thursday should not be frozen")
	}

	// A late Sep 17 row: the surviving day recomputes (12), but the frozen
	// weekly total must stay at the complete value 105 rather than being
	// rebuilt from the surviving raw rows (which would give only 12).
	e.mustIngest(p101, "pl-3",
		snapAt("WS-101", 101, utc(2026, 9, 17, 15, 0), "Chrome", 7))
	if d, _ := testsupport.GetDaily(t, e.DB, 101, "2026-09-15"); d.TotalCount != 100 {
		t.Fatalf("purged date touched: %d", d.TotalCount)
	}
	if d, _ := testsupport.GetDaily(t, e.DB, 101, "2026-09-17"); d.TotalCount != 12 {
		t.Fatalf("surviving date = %d, want 12", d.TotalCount)
	}
	w, _ = testsupport.GetWeekly(t, e.DB, 1, "2026-09-14")
	if w.TotalCount != 105 || !w.Frozen {
		t.Fatalf("frozen week overwritten by partial raw data: %+v", w)
	}

	// Even an explicit rebuild of the surviving day cannot recompute the frozen
	// week silently; it is refused because the week is frozen.
	if _, err := e.Admin.Rebuild(context.Background(),
		admin.RebuildInput{StartDate: "2026-09-17", EndDate: "2026-09-17"}); err == nil {
		t.Fatal("rebuild touching frozen week should be refused")
	} else if ae, ok := err.(*admin.APIError); !ok || ae.Code != "rebuild_range_purged" {
		t.Fatalf("error = %v", err)
	}
}

// TestPurgeEntireEmployee freezes everything for an employee whose raw rows
// were entirely purged, and keeps their historical summaries regardless of
// later ingests in other departments.
func TestPurgeEntireEmployee(t *testing.T) {
	e := setup(t)
	e.publishWidePolicy("00:00", "24:00")
	// Engineering emp 101 (NY) and Sales emp 103 (Berlin), same UTC week.
	e.mustIngest(p101, "pe-1",
		snapAt("WS-101", 101, utc(2026, 9, 15, 14, 0), "Chrome", 100))
	e.mustIngest(p103, "pe-2",
		snapAt("WS-103", 103, utc(2026, 9, 15, 14, 0), "Chrome", 40))

	if _, err := e.Admin.Purge(context.Background(),
		adminPurge("2026-09-30T00:00:00Z")); err != nil {
		t.Fatal(err)
	}
	if n := testsupport.RawCount(t, e.DB, "WS-101"); n != 0 {
		t.Fatalf("raw rows remain: %d", n)
	}
	// Both daily rows and both department weeks survive, frozen.
	if d, ok := testsupport.GetDaily(t, e.DB, 101, "2026-09-15"); !ok || d.TotalCount != 100 || !d.Frozen {
		t.Fatalf("emp101 daily = %+v ok=%v", d, ok)
	}
	if d, ok := testsupport.GetDaily(t, e.DB, 103, "2026-09-15"); !ok || d.TotalCount != 40 || !d.Frozen {
		t.Fatalf("emp103 daily = %+v ok=%v", d, ok)
	}
	if w, ok := testsupport.GetWeekly(t, e.DB, 1, "2026-09-14"); !ok || w.TotalCount != 100 || !w.Frozen {
		t.Fatalf("eng weekly = %+v ok=%v", w, ok)
	}
	if w, ok := testsupport.GetWeekly(t, e.DB, 2, "2026-09-14"); !ok || w.TotalCount != 40 || !w.Frozen {
		t.Fatalf("sales weekly = %+v ok=%v", w, ok)
	}
	// A new sales row in the frozen week is stored and daily recomputed, but the
	// frozen weekly total is preserved.
	e.mustIngest(p103, "pe-3",
		snapAt("WS-103", 103, utc(2026, 9, 16, 14, 0), "Chrome", 5))
	w, _ := testsupport.GetWeekly(t, e.DB, 2, "2026-09-14")
	if w.TotalCount != 40 || !w.Frozen {
		t.Fatalf("frozen sales week changed by new ingest: %+v", w)
	}
}
