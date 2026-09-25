package tailsampling

import (
	"testing"
	"time"
)

// TestRestartReplaysDecisionsAndRearmsOpenTraces verifies local persistence:
// decided traces come back queryable, and a trace still open at crash time is
// re-armed with its remaining TTL and finalized correctly after restart.
func TestRestartReplaysDecisionsAndRearmsOpenTraces(t *testing.T) {
	cfg := testConfig()
	dir := t.TempDir()
	cfg.DataDir = dir

	store1, err := OpenFileStore(dir)
	if err != nil {
		t.Fatal(err)
	}
	clk := NewFakeClock(time.UnixMilli(1_700_000_000_000))
	agg1, err := NewAggregator(cfg, clk, store1)
	if err != nil {
		t.Fatal(err)
	}
	base := clk.Now().UnixMilli()

	// A complete error trace that gets finalized + persisted.
	agg1.Ingest([]Span{
		span("done", "done-r", "", StatusError, base, 10),
		span("done", "done-c", "done-r", StatusOK, base+1, 5),
	})
	advance(clk, agg1, cfg.WaitWindow+time.Millisecond)
	d1 := agg1.Decision("done")
	if d1 == nil || !d1.Kept {
		t.Fatal("setup: expected persisted error keep")
	}

	// An open, incomplete trace, snapshotted mid-flight.
	agg1.Ingest([]Span{span("open", "open-c", "open-r", StatusOK, base+100, 10)})
	advance(clk, agg1, 10*time.Millisecond)
	agg1.SnapshotNow()

	// Simulate crash.
	agg1.Close()
	if err := store1.Close(); err != nil {
		t.Fatal(err)
	}

	// Restart with the same dir/clock; replay decisions, re-arm TTL.
	store2, err := OpenFileStore(dir)
	if err != nil {
		t.Fatal(err)
	}
	agg2, err := NewAggregator(cfg, clk, store2)
	if err != nil {
		t.Fatal(err)
	}

	if d := agg2.Decision("done"); d == nil || !d.Kept || d.ErrorCount != 1 {
		t.Fatalf("decided trace did not survive restart: %+v", d)
	}
	if agg2.Stats().TotalDecided != 1 {
		t.Fatalf("stats counters not replayed: %+v", agg2.Stats())
	}
	if n := agg2.Stats().OpenTraces; n != 1 {
		t.Fatalf("open trace not restored from snapshot, open=%d", n)
	}

	// TTL was armed at ingest (deadline = base+TTL). Advance the rest.
	advance(clk, agg2, cfg.MaxTTL)
	d := agg2.Decision("open")
	if d == nil {
		t.Fatal("restored open trace never finalized")
	}
	if d.Complete || d.ReasonCode != ReasonForcedIncomplete {
		t.Fatalf("restored trace finalized wrong: %+v", d)
	}
	if d.SpanCount != 1 {
		t.Fatal("restored trace lost buffered spans")
	}
	agg2.Close()
	_ = store2.Close()
}

// TestLateArrivalLoggedPersistently checks late_arrivals.jsonl survives restart.
func TestLateArrivalLoggedPersistently(t *testing.T) {
	cfg := testConfig()
	dir := t.TempDir()
	cfg.DataDir = dir

	clk := NewFakeClock(time.UnixMilli(1_700_000_000_000))
	store1, _ := OpenFileStore(dir)
	agg1, _ := NewAggregator(cfg, clk, store1)
	base := clk.Now().UnixMilli()
	agg1.Ingest([]Span{span("p", "p-r", "", StatusOK, base, 5)})
	advance(clk, agg1, cfg.WaitWindow+time.Millisecond)
	agg1.Ingest([]Span{span("p", "p-late", "p-r", StatusError, base+50, 5)})
	agg1.Close()
	_ = store1.Close()

	store2, _ := OpenFileStore(dir)
	agg2, _ := NewAggregator(cfg, clk, store2)
	d := agg2.Decision("p")
	if d == nil || len(d.LateSpans) != 1 {
		t.Fatalf("late arrival did not replay onto decision: %+v", d)
	}
	if agg2.Stats().LateArrivals != 1 {
		t.Fatalf("late counter not replayed: %+v", agg2.Stats())
	}
	agg2.Close()
	_ = store2.Close()
}
