package integration

import (
	"context"
	"testing"

	"desklens/internal/model"
	"desklens/internal/testsupport"
)

// TestPrivacyFiltering verifies the three ingest-time filters: exempt
// department, excluded app and outside monitoring window. Filtered snapshots
// must never reach activity_snapshots.
func TestPrivacyFiltering(t *testing.T) {
	e := setup(t)

	// 2026-09-15 (Tue), seeded v1 window 08:00-18:00 employee-local.
	inWindowNY := utc(2026, 9, 15, 14, 0)  // 10:00 EDT
	outWindowNY := utc(2026, 9, 15, 23, 0) // 19:00 EDT
	excludedApp := utc(2026, 9, 15, 15, 0) // 11:00 EDT

	// 1. Exempt department (Customer Success, employee 104): everything dropped
	// before storage, even inside the window.
	out := e.mustIngest(p104, "b-exempt-dept",
		snapAt("WS-104", 104, inWindowNY, "Chrome", 10))
	if out.Filtered != 1 || out.Accepted != 0 {
		t.Fatalf("exempt dept outcome = %+v", out)
	}
	if out.Items[0].Reason != "exempt_department" {
		t.Fatalf("reason = %s", out.Items[0].Reason)
	}
	if n := testsupport.RawCount(t, e.DB, "WS-104"); n != 0 {
		t.Fatalf("exempt dept rows stored: %d", n)
	}

	// 2. Excluded app (1Password matches 1password*, case-insensitive).
	out = e.mustIngest(p101, "b-excluded-app",
		snapAt("WS-101", 101, excludedApp, "1Password 8", 5))
	if out.Filtered != 1 || out.Items[0].Reason != "excluded_app" {
		t.Fatalf("excluded app outcome = %+v", out)
	}
	if n := testsupport.RawCount(t, e.DB, "WS-101"); n != 0 {
		t.Fatalf("excluded app row stored")
	}

	// 3. Outside monitoring window.
	out = e.mustIngest(p101, "b-outside-window",
		snapAt("WS-101", 101, outWindowNY, "Chrome", 5))
	if out.Filtered != 1 || out.Items[0].Reason != "outside_window" {
		t.Fatalf("outside window outcome = %+v", out)
	}
	if n := testsupport.RawCount(t, e.DB, "WS-101"); n != 0 {
		t.Fatalf("outside-window row stored")
	}

	// 4. Accepted in-window productive snapshot, mixed in the same batch with
	// filtered rows. A filtered row must not roll the batch back.
	out = e.mustIngest(p101, "b-mixed",
		snapAt("WS-101", 101, inWindowNY, "Chrome", 7),
		snapAt("WS-101", 101, excludedApp, "1Password 8", 5),
		snapAt("WS-101", 101, outWindowNY, "WeChat", 5))
	if out.Accepted != 1 || out.Filtered != 2 {
		t.Fatalf("mixed outcome = %+v", out)
	}
	if n := testsupport.RawCount(t, e.DB, "WS-101"); n != 1 {
		t.Fatalf("expected exactly 1 raw row, got %d", n)
	}

	// Filtered rows must not appear in manager detail results either.
	rows, err := e.Manager.DetailRows(context.Background(),
		managerPrincipal(1), 101, utc(2026, 9, 15, 0, 0), utc(2026, 9, 16, 0, 0))
	if err != nil {
		t.Fatal(err)
	}
	for _, r := range rows {
		if r.AppName == "1Password 8" {
			t.Fatal("excluded app leaked into detail rows")
		}
	}
}

// TestIdempotencyAndConflict covers:
//   - identical re-send: processed once (duplicate), counts unchanged
//   - conflicting re-send: whole batch rejected, no partial writes
//   - validation failure: whole batch rolled back
//   - batch_id replay: deterministic previous outcome
func TestIdempotencyAndConflict(t *testing.T) {
	e := setup(t)
	t1 := utc(2026, 9, 15, 14, 0) // 10:00 EDT, in window
	t2 := utc(2026, 9, 15, 14, 1)
	t3 := utc(2026, 9, 15, 14, 2)

	out := e.mustIngest(p101, "dup-batch-1",
		snapAt("WS-101", 101, t1, "Chrome", 10),
		snapAt("WS-101", 101, t2, "Zoom", 4))
	if out.Accepted != 2 {
		t.Fatalf("initial accept = %d", out.Accepted)
	}

	// Identical re-send of the same minutes: duplicates, nothing new stored.
	out = e.mustIngest(p101, "dup-batch-2",
		snapAt("WS-101", 101, t1, "Chrome", 10),
		snapAt("WS-101", 101, t2, "Zoom", 4))
	if out.Accepted != 0 || out.Duplicates != 2 {
		t.Fatalf("re-send outcome = %+v", out)
	}
	if n := testsupport.RawCount(t, e.DB, "WS-101"); n != 2 {
		t.Fatalf("raw count after re-send = %d, want 2", n)
	}
	app, _, ok := e.rawRow("WS-101", t1)
	if !ok || app != "Chrome" {
		t.Fatalf("original row altered: %+v", app)
	}
	// Conflicting content for t1 mixed with a fresh minute t3: entire batch
	// rejected; t3 must NOT be inserted (no partial write).
	e.ingestErr(p101, []model.Snapshot{
		snapAt("WS-101", 101, t1, "Chrome", 999), // count conflict
		snapAt("WS-101", 101, t3, "Firefox", 3),  // would otherwise insert
	}, "snapshot_conflict")
	if n := testsupport.RawCount(t, e.DB, "WS-101"); n != 2 {
		t.Fatalf("partial write after conflict: %d rows", n)
	}
	if _, _, ok := e.rawRow("WS-101", t3); ok {
		t.Fatal("conflict batch partially wrote t3")
	}

	// Validation failure (negative count) rolls back the whole batch.
	e.ingestErr(p101, []model.Snapshot{
		snapAt("WS-101", 101, utc(2026, 9, 15, 15, 0), "Chrome", -1),
		snapAt("WS-101", 101, utc(2026, 9, 15, 15, 1), "Chrome", 5),
	}, "invalid_item")
	if n := testsupport.RawCount(t, e.DB, "WS-101"); n != 2 {
		t.Fatalf("partial write after validation failure: %d", n)
	}

	// Cross-workstation mismatch: key for WS-101 cannot post WS-102 data.
	e.ingestErr(p101, []model.Snapshot{
		snapAt("WS-102", 102, t1, "Chrome", 5),
	}, "workstation_mismatch")

	// Replaying the original committed batch_id returns a replay outcome with
	// the same counters and never double-accumulates.
	replay := e.mustIngest(p101, "dup-batch-1",
		snapAt("WS-101", 101, t1, "Chrome", 10),
		snapAt("WS-101", 101, t2, "Zoom", 4))
	if replay.Accepted != 2 {
		t.Fatalf("replay accepted = %d, want 2 (prior result)", replay.Accepted)
	}
	daily, has := testsupport.GetDaily(t, e.DB, 101, "2026-09-15")
	if !has {
		t.Fatal("daily summary missing")
	}
	if daily.TotalCount != 14 {
		t.Fatalf("daily total = %d, want 14", daily.TotalCount)
	}
}
