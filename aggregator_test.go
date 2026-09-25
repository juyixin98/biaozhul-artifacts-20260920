package tailsampling

import (
	"testing"
	"time"
)

func testConfig() Config {
	cfg := DefaultConfig()
	cfg.DataDir = "" // overridden per test
	cfg.WaitWindow = 50 * time.Millisecond
	cfg.MaxTTL = 100 * time.Millisecond
	cfg.SnapshotInterval = 0 // manual snapshots in tests
	cfg.LatencyThresholdMs = 1000
	cfg.ProbabilisticRate = 0
	cfg.BudgetCapacity = 3
	cfg.BudgetRefillPerSec = 0
	return cfg
}

func newTestAggregator(t *testing.T, cfg Config) (*Aggregator, *fakeClock, *FileStore) {
	t.Helper()
	dir := t.TempDir()
	cfg.DataDir = dir
	store, err := OpenFileStore(dir)
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	clk := NewFakeClock(time.UnixMilli(1_700_000_000_000))
	agg, err := NewAggregator(cfg, clk, store)
	if err != nil {
		t.Fatalf("new aggregator: %v", err)
	}
	t.Cleanup(func() {
		agg.Close()
		_ = store.Close()
	})
	return agg, clk, store
}

func span(traceID, spanID, parent, status string, startMs, durMs int64) Span {
	return Span{
		TraceID:      traceID,
		SpanID:       spanID,
		ParentSpanID: parent,
		Name:         "op-" + spanID,
		Service:      "svc-a",
		Status:       status,
		StartTimeMs:  startMs,
		DurationMs:   durMs,
	}
}

// advance moves the fake clock and then waits for the aggregator to drain
// every finalization the timer callbacks enqueued.
func advance(clk *fakeClock, agg *Aggregator, d time.Duration) {
	clk.Advance(d)
	agg.WaitIdle()
}

func TestHashStableAndBounded(t *testing.T) {
	h1 := hashTraceID("abc")
	h2 := hashTraceID("abc")
	if h1 != h2 {
		t.Fatalf("hash not deterministic: %v != %v", h1, h2)
	}
	for _, id := range []string{"a", "b", "c", "d", "e"} {
		if h := hashTraceID(id); h < 0 || h >= 1 {
			t.Fatalf("hash out of range: %v", h)
		}
	}
}

func TestTokenBucketExhaustionAndRefill(t *testing.T) {
	start := time.UnixMilli(0)
	b := NewTokenBucket(2, 1, start)
	if !b.tryTake(start) || !b.tryTake(start) {
		t.Fatal("expected two tokens")
	}
	if b.tryTake(start) {
		t.Fatal("expected bucket exhausted")
	}
	if !b.tryTake(start.Add(1010 * time.Millisecond)) {
		t.Fatal("expected refill to grant token after ~1s at 1 token/s")
	}
}

func TestPolicyOrdering_ErrorBeatsLatency(t *testing.T) {
	cfg := testConfig()
	bucket := NewTokenBucket(cfg.BudgetCapacity, 0, time.UnixMilli(0))
	s := NewSampler(cfg, bucket)
	now := time.UnixMilli(1000)
	d, _ := s.Evaluate(traceInput{
		traceID: "t", errorCount: 1, durationMs: 5000,
		startMs: 0, endMs: 5000, complete: true, rootSpanID: "r", now: now,
	})
	if !d.Kept || d.Policy != "error" || d.ReasonCode != ReasonErrorKeep {
		t.Fatalf("want error keep, got kept=%v policy=%s", d.Kept, d.Policy)
	}
}

func TestPolicyLatencyThenProbabilisticThenDrop(t *testing.T) {
	cfg := testConfig()
	cfg.ProbabilisticRate = 1 // everything else kept
	bucket := NewTokenBucket(100, 0, time.UnixMilli(0))
	s := NewSampler(cfg, bucket)
	now := time.UnixMilli(1000)
	base := traceInput{traceID: "x", startMs: 0, endMs: 100, complete: true, now: now}

	in := base
	in.durationMs = 5000
	d, _ := s.Evaluate(in)
	if d.Policy != "latency" || !d.Kept {
		t.Fatalf("want latency keep, got %+v", d)
	}

	// force a non-latency, deterministic probabilistic miss: rate 0 in cfg.
	cfg2 := testConfig()
	s2 := NewSampler(cfg2, NewTokenBucket(100, 0, now))
	in2 := base
	in2.traceID = "short-trace"
	d2, _ := s2.Evaluate(in2)
	if d2.Kept || d2.ReasonCode != ReasonDefaultDrop {
		t.Fatalf("want default drop, got kept=%v reason=%s", d2.Kept, d2.ReasonCode)
	}
}

func TestBudgetDegradation_LatencyDowngraded_ErrorStillKept(t *testing.T) {
	cfg := testConfig()
	cfg.LatencyThresholdMs = 10 // everything is "slow"
	cfg.BudgetCapacity = 2
	agg, clk, _ := newTestAggregator(t, cfg)
	base := clk.Now().UnixMilli()

	// Two slow traces consume the budget.
	for i := 0; i < 2; i++ {
		id := "slow" + string(rune('A'+i))
		agg.Ingest([]Span{
			span(id, id+"-r", "", StatusOK, base+1, 100),
			span(id, id+"-c", id+"-r", StatusOK, base+2, 50),
		})
		advance(clk, agg, cfg.WaitWindow+time.Millisecond)
		d := agg.Decision(id)
		if d == nil || !d.Kept {
			t.Fatalf("trace %s should be kept within budget", id)
		}
	}

	// Third slow trace: budget exhausted -> explicit degraded DROP.
	agg.Ingest([]Span{span("slowC", "slowC-r", "", StatusOK, base+3, 100)})
	advance(clk, agg, cfg.WaitWindow+time.Millisecond)
	d := agg.Decision("slowC")
	if d == nil {
		t.Fatal("missing decision slowC")
	}
	if d.Kept {
		t.Fatalf("slowC must be downgraded to drop when budget exhausted")
	}
	if !d.Degraded || d.ReasonCode != ReasonBudgetDrop {
		t.Fatalf("want degraded BUDGET_DROP, got degraded=%v code=%s", d.Degraded, d.ReasonCode)
	}

	// Error trace while exhausted: kept beyond budget, marked degraded.
	agg.Ingest([]Span{
		span("errX", "errX-r", "", StatusOK, base+4, 100),
		span("errX", "errX-c", "errX-r", StatusError, base+5, 20),
	})
	advance(clk, agg, cfg.WaitWindow+time.Millisecond)
	de := agg.Decision("errX")
	if de == nil || !de.Kept {
		t.Fatalf("error trace must remain kept even when budget is exhausted")
	}
	if !de.Degraded || de.Policy != "error" {
		t.Fatalf("error keep should be flagged degraded, got %+v", de)
	}

	st := agg.Stats()
	if st.DegradedDowngrades != 1 || st.DegradedOverBudgetKeeps != 1 {
		t.Fatalf("want 1 downgrade + 1 over-budget keep, got %+v", st)
	}
	if st.BudgetObservations.Tokens >= 1 {
		t.Fatalf("budget should report exhausted, got %+v", st.BudgetObservations)
	}
}

func TestLateErrorSpanWithinWindowChangesOutcome(t *testing.T) {
	cfg := testConfig()
	cfg.ProbabilisticRate = 0
	cfg.LatencyThresholdMs = 1_000_000 // short trace -> would default-drop
	agg, clk, _ := newTestAggregator(t, cfg)
	base := clk.Now().UnixMilli()

	// Complete-but-healthy trace arrives; wait window starts.
	agg.Ingest([]Span{
		span("late-err", "root", "", StatusOK, base, 10),
		span("late-err", "child", "root", StatusOK, base+1, 5),
	})
	// Error span arrives inside the wait window (the point of waiting).
	advance(clk, agg, cfg.WaitWindow/2)
	rep := agg.Ingest([]Span{span("late-err", "err-child", "root", StatusError, base+3, 5)})
	if rep.Accepted != 1 {
		t.Fatalf("error span should be accepted within window, report=%+v", rep)
	}
	advance(clk, agg, cfg.WaitWindow/2+time.Millisecond)
	d := agg.Decision("late-err")
	if d == nil {
		t.Fatal("decision expected")
	}
	if !d.Kept || d.ErrorCount != 1 || d.Policy != "error" {
		t.Fatalf("late in-window error should flip decision to error-keep, got %+v", d)
	}
}

func TestDecisionImmutableAndLateAfterDecisionRecorded(t *testing.T) {
	cfg := testConfig()
	cfg.LatencyThresholdMs = 1_000_000
	agg, clk, _ := newTestAggregator(t, cfg)
	base := clk.Now().UnixMilli()

	agg.Ingest([]Span{span("frozen", "root", "", StatusOK, base, 10)})
	advance(clk, agg, cfg.WaitWindow+time.Millisecond)
	first := agg.Decision("frozen")
	if first == nil || first.Kept {
		t.Fatal("expected default-drop decision")
	}
	firstJSON := first.Reason

	// A late ERROR span after finalization must not flip the decision.
	rep := agg.Ingest([]Span{span("frozen", "late-err", "root", StatusError, base+50, 5)})
	if rep.Late != 1 {
		t.Fatalf("expected late=1, got %+v", rep)
	}
	again := agg.Decision("frozen")
	if again == nil || again.Kept != false {
		t.Fatal("decision changed after finalization")
	}
	if again.DecidedAtMs != first.DecidedAtMs {
		t.Fatal("DecidedAtMs must stay identical (single immutable decision)")
	}
	if len(again.LateSpans) != 1 || again.LateSpans[0].SpanID != "late-err" {
		t.Fatalf("late span not recorded on decision: %+v", again.LateSpans)
	}
	if again.Reason != firstJSON {
		t.Fatal("reason mutated after decision")
	}
}

func TestIncompleteTraceMarkedOnTTL(t *testing.T) {
	cfg := testConfig()
	agg, clk, _ := newTestAggregator(t, cfg)
	base := clk.Now().UnixMilli()

	// Only a child span; no root ever arrives.
	agg.Ingest([]Span{span("orphan", "child-only", "missing-root", StatusOK, base, 10)})
	if d := agg.Decision("orphan"); d != nil {
		t.Fatal("decision should not exist before TTL")
	}
	advance(clk, agg, cfg.MaxTTL+time.Millisecond)
	d := agg.Decision("orphan")
	if d == nil {
		t.Fatal("expected TTL finalization")
	}
	if d.Complete {
		t.Fatal("trace must be marked incomplete")
	}
	if d.RootSpanID != "" || d.SpanCount != 1 {
		t.Fatalf("partial data mismatch: %+v", d)
	}
	if d.ReasonCode != ReasonForcedIncomplete {
		t.Fatalf("want FORCED_INCOMPLETE, got %s", d.ReasonCode)
	}
	st := agg.Stats()
	if st.Incomplete != 1 {
		t.Fatalf("want 1 incomplete, got %d", st.Incomplete)
	}
}

func TestLargeTraceAggregatesCorrectly(t *testing.T) {
	cfg := testConfig()
	cfg.BudgetCapacity = 10
	agg, clk, _ := newTestAggregator(t, cfg)
	base := clk.Now().UnixMilli()

	const n = 2000
	spans := make([]Span, 0, n+1)
	spans = append(spans, span("big", "root", "", StatusOK, base, 3000))
	for i := 0; i < n; i++ {
		st := StatusOK
		if i%250 == 0 {
			st = StatusError
		}
		spans = append(spans, span("big", spanIDFor(i), "root", st, base+int64(i), 5))
	}
	rep := agg.Ingest(spans)
	if rep.Accepted != n+1 {
		t.Fatalf("accepted=%d want %d", rep.Accepted, n+1)
	}

	// Duplicate re-ingest is rejected.
	dup := agg.Ingest(spans[:2])
	if dup.Duplicates != 2 {
		t.Fatalf("duplicates=%d want 2", dup.Duplicates)
	}

	advance(clk, agg, cfg.WaitWindow+time.Millisecond)
	d := agg.Decision("big")
	if d == nil || !d.Kept {
		t.Fatalf("big error trace must keep, got %+v", d)
	}
	if d.SpanCount != n+1 {
		t.Fatalf("span count=%d want %d", d.SpanCount, n+1)
	}
	if d.ErrorCount != n/250 {
		t.Fatalf("error count=%d want %d", d.ErrorCount, n/250)
	}
	if d.DurationMs < 3000 {
		t.Fatalf("duration should span >= 3000ms, got %d", d.DurationMs)
	}
}

func spanIDFor(i int) string {
	const letters = "abcdefghijklmnopqrstuvwxyz0123456789"
	id := make([]byte, 0, 4)
	for {
		id = append(id, letters[i%len(letters)])
		i /= len(letters)
		if i == 0 {
			break
		}
	}
	return string(id)
}

func TestForcedFlushMarksIncomplete(t *testing.T) {
	cfg := testConfig()
	agg, _, _ := newTestAggregator(t, cfg)
	base := time.Now().UnixMilli()
	agg.Ingest([]Span{span("f", "child", "root", StatusOK, base, 10)})
	out := agg.Flush()
	if len(out) != 1 || out[0].Complete || out[0].ReasonCode != ReasonForcedIncomplete {
		t.Fatalf("flush should mark incomplete: %+v", out)
	}
}
