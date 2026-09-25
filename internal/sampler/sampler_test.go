package sampler

import (
	"sync"
	"testing"
	"time"
)

type fakeClock struct {
	mu sync.Mutex
	t  time.Time
}

func newFakeClock() *fakeClock { return &fakeClock{t: time.Date(2026, 9, 24, 12, 0, 0, 0, time.UTC)} }

func (f *fakeClock) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.t
}

func (f *fakeClock) Advance(d time.Duration) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.t = f.t.Add(d)
}

func testConfig() Config {
	return Config{
		DecisionWait:       10 * time.Second,
		LatencyThresholdMs: 500,
		BudgetKeepsPerMin:  0, // unlimited unless a test sets it
		MaxInflightTraces:  100,
		DecisionTTL:        100 * time.Second,
	}
}

func mkSpan(trace, spanID, parent, status string, startMs, durMs int64) Span {
	return Span{TraceID: trace, SpanID: spanID, ParentID: parent,
		Service: "svc", Name: "op", StartUnixMs: startMs, DurationMs: durMs, Status: status}
}

func decideAll(t *testing.T, s *Sampler, c *fakeClock) {
	t.Helper()
	c.Advance(11 * time.Second) // past the 10s decision wait
	s.DecideDue()
}

func TestErrorPolicyKeeps(t *testing.T) {
	c := newFakeClock()
	s := New(testConfig(), nil, c)
	s.Ingest([]Span{
		mkSpan("t1", "r", "", "ok", 1000, 50),
		mkSpan("t1", "c", "r", "error", 1010, 20),
	})
	decideAll(t, s, c)
	d, ok := s.GetDecision("t1")
	if !ok {
		t.Fatal("no decision recorded")
	}
	if !d.Keep {
		t.Fatalf("expected keep, got drop: %+v", d)
	}
	if !contains(d.Reasons, "error_span") {
		t.Fatalf("expected error_span reason, got %v", d.Reasons)
	}
	if d.Incomplete {
		t.Fatalf("complete trace flagged incomplete: %v", d.IncompleteReasons)
	}
}

func TestLatencyPolicyKeeps(t *testing.T) {
	c := newFakeClock()
	s := New(testConfig(), nil, c)
	s.Ingest([]Span{
		mkSpan("t2", "r", "", "ok", 1000, 900),
		mkSpan("t2", "c", "r", "ok", 1500, 100),
	})
	decideAll(t, s, c)
	d, _ := s.GetDecision("t2")
	if !d.Keep {
		t.Fatalf("expected keep for slow trace: %+v", d)
	}
	if len(d.Reasons) == 0 || d.Reasons[0] != "latency_exceeded(900ms>=500ms)" {
		t.Fatalf("unexpected reasons: %v", d.Reasons)
	}
}

func TestNoPolicyDrops(t *testing.T) {
	c := newFakeClock()
	s := New(testConfig(), nil, c)
	s.Ingest([]Span{mkSpan("t3", "r", "", "ok", 1000, 20)})
	decideAll(t, s, c)
	d, _ := s.GetDecision("t3")
	if d.Keep {
		t.Fatalf("expected drop: %+v", d)
	}
	if !contains(d.Reasons, "no_policy_matched") {
		t.Fatalf("expected no_policy_matched, got %v", d.Reasons)
	}
}

func TestLateSpanKeepsConsistentDecision(t *testing.T) {
	c := newFakeClock()
	s := New(testConfig(), nil, c)
	s.Ingest([]Span{mkSpan("t4", "r", "", "ok", 1000, 20)})
	decideAll(t, s, c)
	d1, _ := s.GetDecision("t4")
	if d1.Keep {
		t.Fatal("precondition: trace should be dropped")
	}

	// An error span arrives AFTER the drop decision.
	st := s.Ingest([]Span{mkSpan("t4", "late", "r", "error", 1020, 5)})
	if st["t4"] != StatusDrop {
		t.Fatalf("late span must get the original drop verdict, got %s", st["t4"])
	}
	d2, _ := s.GetDecision("t4")
	if d2.Keep != d1.Keep {
		t.Fatalf("decision flipped: was %v now %v", d1.Keep, d2.Keep)
	}
	if d2.LateSpans != 1 {
		t.Fatalf("expected 1 late span, got %d", d2.LateSpans)
	}
	if !d2.Incomplete || !contains(d2.IncompleteReasons, "late_span_after_decision") {
		t.Fatalf("expected incomplete with late_span_after_decision: %+v", d2)
	}
}

func TestLateSpanOnKeptTraceIsStored(t *testing.T) {
	c := newFakeClock()
	s := New(testConfig(), nil, c)
	s.Ingest([]Span{mkSpan("t5", "r", "", "error", 1000, 20)})
	decideAll(t, s, c)
	st := s.Ingest([]Span{mkSpan("t5", "late", "r", "ok", 1010, 5)})
	if st["t5"] != StatusKeep {
		t.Fatalf("expected keep status, got %s", st["t5"])
	}
	spans, ok := s.GetTrace("t5")
	if !ok || len(spans) != 2 {
		t.Fatalf("kept trace should hold 2 spans incl. late one, got %d", len(spans))
	}
	d, _ := s.GetDecision("t5")
	if d.LateSpans != 1 || !d.Incomplete {
		t.Fatalf("late span not reflected: %+v", d)
	}
}

func TestIncompleteMarkingMissingRoot(t *testing.T) {
	c := newFakeClock()
	s := New(testConfig(), nil, c)
	// Child span whose parent never arrived.
	s.Ingest([]Span{mkSpan("t6", "c", "ghost", "error", 1000, 20)})
	decideAll(t, s, c)
	d, _ := s.GetDecision("t6")
	if !d.Keep {
		t.Fatal("error trace should be kept")
	}
	if !d.Incomplete {
		t.Fatal("expected incomplete flag")
	}
	if !contains(d.IncompleteReasons, "root_span_missing") {
		t.Fatalf("expected root_span_missing: %v", d.IncompleteReasons)
	}
}

func TestBudgetExhaustionDegrades(t *testing.T) {
	c := newFakeClock()
	cfg := testConfig()
	cfg.BudgetKeepsPerMin = 2
	s := New(cfg, nil, c)
	for _, id := range []string{"b1", "b2", "b3"} {
		s.Ingest([]Span{mkSpan(id, "r", "", "error", 1000, 20)})
	}
	decideAll(t, s, c)

	kept, dropped := 0, 0
	for _, id := range []string{"b1", "b2", "b3"} {
		d, _ := s.GetDecision(id)
		if d.Keep {
			kept++
		} else {
			dropped++
			if !d.Degraded {
				t.Fatalf("budget-exhausted drop must be degraded: %+v", d)
			}
			if !contains(d.Reasons, "budget_exhausted") {
				t.Fatalf("expected budget_exhausted reason: %v", d.Reasons)
			}
		}
	}
	if kept != 2 || dropped != 1 {
		t.Fatalf("expected 2 kept / 1 dropped, got %d/%d", kept, dropped)
	}
	st := s.Stats()
	if st.BudgetExhausted != 1 || st.DegradedDecisions != 1 {
		t.Fatalf("stats mismatch: %+v", st)
	}
}

func TestBudgetWindowRolls(t *testing.T) {
	c := newFakeClock()
	cfg := testConfig()
	cfg.BudgetKeepsPerMin = 1
	s := New(cfg, nil, c)
	s.Ingest([]Span{mkSpan("w1", "r", "", "error", 1000, 20)})
	decideAll(t, s, c)
	if d, _ := s.GetDecision("w1"); !d.Keep {
		t.Fatal("first trace should be kept")
	}
	// Next minute: budget resets.
	c.Advance(time.Minute)
	s.Ingest([]Span{mkSpan("w2", "r", "", "error", 2000, 20)})
	decideAll(t, s, c)
	if d, _ := s.GetDecision("w2"); !d.Keep {
		t.Fatalf("budget should have rolled, w2 dropped: %+v", d)
	}
}

func TestInflightLimitForcesDegradedDecision(t *testing.T) {
	c := newFakeClock()
	cfg := testConfig()
	cfg.MaxInflightTraces = 2
	s := New(cfg, nil, c)
	s.Ingest([]Span{mkSpan("f1", "r", "", "ok", 1000, 20)})
	c.Advance(time.Second)
	s.Ingest([]Span{mkSpan("f2", "r", "", "ok", 1000, 20)})
	c.Advance(time.Second)
	// Third trace pushes f1 out before its wait window elapsed.
	s.Ingest([]Span{mkSpan("f3", "r", "", "ok", 1000, 20)})

	d, ok := s.GetDecision("f1")
	if !ok {
		t.Fatal("f1 should have been force-decided")
	}
	if !d.Degraded || !contains(d.Reasons, "inflight_limit_forced_decision") {
		t.Fatalf("expected degraded forced decision: %+v", d)
	}
	if p := s.PendingTraces(); len(p) != 2 {
		t.Fatalf("expected 2 inflight traces, got %v", p)
	}
}

func TestDecisionTTLEviction(t *testing.T) {
	c := newFakeClock()
	cfg := testConfig()
	cfg.DecisionTTL = 30 * time.Second
	s := New(cfg, nil, c)
	s.Ingest([]Span{mkSpan("ttl1", "r", "", "ok", 1000, 20)})
	decideAll(t, s, c)
	if _, ok := s.GetDecision("ttl1"); !ok {
		t.Fatal("decision should exist")
	}
	c.Advance(31 * time.Second)
	s.DecideDue()
	if _, ok := s.GetDecision("ttl1"); ok {
		t.Fatal("decision should have been evicted after TTL")
	}
}

func TestDecisionConsistencyAcrossRestart(t *testing.T) {
	dir := t.TempDir()
	c := newFakeClock()
	store, err := NewStore(dir)
	if err != nil {
		t.Fatal(err)
	}
	s := New(testConfig(), store, c)
	s.Ingest([]Span{mkSpan("r1", "root", "", "ok", 1000, 20)})
	decideAll(t, s, c)
	store.Close()

	// "Restart": new sampler loads persisted decisions.
	store2, err := NewStore(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer store2.Close()
	prior, err := LoadDecisions(dir)
	if err != nil {
		t.Fatal(err)
	}
	s2 := New(testConfig(), store2, c)
	s2.LoadDecisions(prior)

	st := s2.Ingest([]Span{mkSpan("r1", "late", "root", "error", 1010, 5)})
	if st["r1"] != StatusDrop {
		t.Fatalf("after restart late span must keep original drop verdict, got %s", st["r1"])
	}
	d, _ := s2.GetDecision("r1")
	if d.Keep || d.LateSpans != 1 {
		t.Fatalf("inconsistent after restart: %+v", d)
	}
}

func TestKeptTraceSpansQueryable(t *testing.T) {
	c := newFakeClock()
	s := New(testConfig(), nil, c)
	s.Ingest([]Span{
		mkSpan("k1", "r", "", "error", 1000, 50),
		mkSpan("k1", "c", "r", "ok", 1010, 10),
	})
	decideAll(t, s, c)
	spans, ok := s.GetTrace("k1")
	if !ok || len(spans) != 2 {
		t.Fatalf("expected 2 kept spans, got %v", spans)
	}
	if _, ok := s.GetTrace("unknown"); ok {
		t.Fatal("unknown trace should not be queryable")
	}
}
