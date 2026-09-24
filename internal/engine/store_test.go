package engine

import (
	"path/filepath"
	"testing"
	"time"
)

func TestFileStoreRoundTripAndCatchUpOnRestart(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "state.json")

	// Phase 1: run a service for a while, persist, then "crash".
	clk := NewFakeClock(utcT(2024, 6, 1, 10, 0))
	store := NewFileStore(path)
	e1 := newTestEngine(t, clk, 3)
	e1.store = store
	if _, err := e1.CreateSchedule(CreateInput{
		ID: "job", Minute: "0", Hour: "*", Weekday: "*", Timezone: "UTC", Enabled: enabled(),
	}); err != nil {
		t.Fatal(err)
	}
	clk.Set(utcT(2024, 6, 1, 12, 0))
	res1 := e1.Advance(clk.Now())
	if len(res1.Fired) != 2 { // 11:00, 12:00
		t.Fatalf("phase1 fires=%d", len(res1.Fired))
	}

	// Phase 2: long downtime (24 hours), a brand new engine restores state.
	clk2 := NewFakeClock(utcT(2024, 6, 2, 12, 0))
	snap, err := LoadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if snap == nil || snap.TZData != "2024a" {
		t.Fatalf("snapshot wrong: %+v", snap)
	}
	e2 := newTestEngine(t, clk2, 3)
	e2.store = store
	e2.Restore(snap)

	if got := len(e2.List()); got != 1 {
		t.Fatalf("restored schedule count=%d", got)
	}
	res2 := e2.Advance(clk2.Now())
	// Watermark was 12:00 on Jun 1; due since = Jun1 13:00 .. Jun2 12:00 = 24
	// firings. Cap is 3, so exactly 3 delivered and 21 skipped.
	if len(res2.Fired) != 3 {
		t.Fatalf("post-restart fires=%d (%v)", len(res2.Fired), res2.Fired)
	}
	if len(res2.Skipped) != 1 || res2.Skipped[0].Count != 21 {
		t.Fatalf("post-restart skipped=%+v", res2.Skipped)
	}
	// Old delivered firings must not replay.
	for _, f := range res2.Fired {
		if !f.EventTime.After(utcT(2024, 6, 2, 9, 0)) {
			t.Fatalf("stale fire replayed: %s", f.EventTime)
		}
	}

	// Restarting a third time with no elapsed time delivers nothing.
	snap2, _ := LoadFile(path)
	e3 := newTestEngine(t, clk2, 3)
	e3.store = store
	e3.Restore(snap2)
	res3 := e3.Advance(clk2.Now())
	if len(res3.Fired) != 0 {
		t.Fatalf("third restart re-fired %d events", len(res3.Fired))
	}
}

func TestMissingFileLoadsNil(t *testing.T) {
	snap, err := LoadFile(filepath.Join(t.TempDir(), "absent.json"))
	if err != nil || snap != nil {
		t.Fatalf("want (nil,nil), got (%v,%v)", snap, err)
	}
}

func TestSnapshotTimeIsUTC(t *testing.T) {
	clk := NewFakeClock(time.Now())
	e := newTestEngine(t, clk, 5)
	snap := e.Snapshot()
	if snap.ExportedAt.Location() != time.UTC {
		t.Fatalf("snapshot time not UTC: %v", snap.ExportedAt.Location())
	}
}
