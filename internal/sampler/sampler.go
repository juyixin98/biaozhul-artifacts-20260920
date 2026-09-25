package sampler

import (
	"sort"
	"sync"
	"time"
)

// Clock abstracts time so tests can drive decisions deterministically.
type Clock interface{ Now() time.Time }

type realClock struct{}

func (realClock) Now() time.Time { return time.Now() }

// traceBuf accumulates spans of one undecided trace.
type traceBuf struct {
	spans     []Span
	firstSeen time.Time
}

// Sampler is the tail-sampling engine: it buffers spans per trace for the
// decision wait window, then applies error/latency/budget policies and
// records one final, consistent decision per trace.
type Sampler struct {
	cfg   Config
	clock Clock
	store *Store

	mu       sync.Mutex
	inflight map[string]*traceBuf
	decided  map[string]*Decision // final decisions, kept for DecisionTTL
	decOrder []string             // decided trace IDs in decision order (for TTL eviction)
	kept     map[string][]Span    // spans of kept traces

	budgetWindowStart time.Time
	budgetUsed        int

	stats Stats
}

// New creates a Sampler. clock may be nil for a real-time clock.
func New(cfg Config, store *Store, clock Clock) *Sampler {
	if clock == nil {
		clock = realClock{}
	}
	s := &Sampler{
		cfg:      cfg.withDefaults(),
		clock:    clock,
		store:    store,
		inflight: map[string]*traceBuf{},
		decided:  map[string]*Decision{},
		kept:     map[string][]Span{},
	}
	s.budgetWindowStart = clock.Now()
	return s
}

// LoadDecisions restores previously persisted decisions (within TTL) so a
// restart does not break decision consistency for late spans. It returns
// how many decisions were actually restored.
func (s *Sampler) LoadDecisions(prior map[string]Decision) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.clock.Now()
	ids := make([]string, 0, len(prior))
	for id := range prior {
		ids = append(ids, id)
	}
	sort.Slice(ids, func(i, j int) bool { return prior[ids[i]].DecidedAt.Before(prior[ids[j]].DecidedAt) })
	restored := 0
	for _, id := range ids {
		d := prior[id]
		if now.Sub(d.DecidedAt) > s.cfg.DecisionTTL {
			continue
		}
		cp := d
		s.decided[id] = &cp
		s.decOrder = append(s.decOrder, id)
		restored++
	}
	return restored
}

// Ingest buffers a batch of spans and returns the current per-trace status.
// Spans of an already-decided trace get the identical stored verdict
// (consistency) and are counted as late spans.
func (s *Sampler) Ingest(spans []Span) map[string]IngestStatus {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := map[string]IngestStatus{}
	now := s.clock.Now()
	for _, sp := range spans {
		s.stats.SpansReceived++
		if d, ok := s.decided[sp.TraceID]; ok {
			// Late span: same final decision, mark trace incomplete.
			d.LateSpans++
			d.Incomplete = true
			if !contains(d.IncompleteReasons, "late_span_after_decision") {
				d.IncompleteReasons = append(d.IncompleteReasons, "late_span_after_decision")
			}
			s.stats.LateSpans++
			if d.Keep {
				s.kept[sp.TraceID] = append(s.kept[sp.TraceID], sp)
				s.store.AppendSpans([]Span{sp})
			}
			if d.Keep {
				out[sp.TraceID] = StatusKeep
			} else {
				out[sp.TraceID] = StatusDrop
			}
			continue
		}
		buf, ok := s.inflight[sp.TraceID]
		if !ok {
			buf = &traceBuf{firstSeen: now}
			s.inflight[sp.TraceID] = buf
		}
		buf.spans = append(buf.spans, sp)
		if _, pending := out[sp.TraceID]; !pending {
			out[sp.TraceID] = StatusPending
		}
	}
	s.enforceInflightLimit(now)
	return out
}

// DecideDue finalizes every trace whose wait window has expired. Called by
// the background ticker in production and directly by tests.
func (s *Sampler) DecideDue() {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.clock.Now()
	var due []string
	for id, buf := range s.inflight {
		if now.Sub(buf.firstSeen) >= s.cfg.DecisionWait {
			due = append(due, id)
		}
	}
	sort.Strings(due) // deterministic order
	for _, id := range due {
		s.decide(id, now, false)
	}
	s.evictExpired(now)
}

// decide makes the final decision for one inflight trace. forced=true means
// the in-flight limit pushed this trace out before its wait window elapsed.
// Caller must hold s.mu.
func (s *Sampler) decide(traceID string, now time.Time, forced bool) {
	buf, ok := s.inflight[traceID]
	if !ok {
		return
	}
	delete(s.inflight, traceID)

	keep, reasons := evaluatePolicies(buf.spans, s.cfg)
	degraded := false

	if keep && s.budgetExhausted(now) {
		// Explicit degradation: budget spent, trace that matched a keep
		// policy is dropped with a recorded reason.
		keep = false
		degraded = true
		reasons = append(reasons, "budget_exhausted")
		s.stats.BudgetExhausted++
	}
	if forced {
		degraded = true
		reasons = append(reasons, "inflight_limit_forced_decision")
		s.stats.InflightForced++
	}
	if !keep && len(reasons) == 0 {
		reasons = append(reasons, "no_policy_matched")
	}

	incompleteReasons := completeness(buf.spans)
	d := &Decision{
		TraceID:           traceID,
		Keep:              keep,
		Reasons:           reasons,
		Degraded:          degraded,
		Incomplete:        len(incompleteReasons) > 0,
		IncompleteReasons: incompleteReasons,
		SpanCount:         len(buf.spans),
		DecidedAt:         now,
	}
	s.decided[traceID] = d
	s.decOrder = append(s.decOrder, traceID)

	s.stats.TracesDecided++
	if keep {
		s.stats.TracesKept++
		s.budgetUsed++
		s.kept[traceID] = append([]Span(nil), buf.spans...)
		s.store.AppendSpans(buf.spans)
	} else {
		s.stats.TracesDropped++
	}
	if degraded {
		s.stats.DegradedDecisions++
	}
	s.store.AppendDecision(*d)
}

// enforceInflightLimit force-decides the oldest traces when the in-flight
// cap is exceeded. Caller must hold s.mu.
func (s *Sampler) enforceInflightLimit(now time.Time) {
	for len(s.inflight) > s.cfg.MaxInflightTraces {
		oldest, oldestAt := "", now
		for id, buf := range s.inflight {
			if oldest == "" || buf.firstSeen.Before(oldestAt) {
				oldest, oldestAt = id, buf.firstSeen
			}
		}
		if oldest == "" {
			return
		}
		s.decide(oldest, now, true)
	}
}

// budgetExhausted reports whether the per-minute keep budget is spent,
// rolling the window forward when needed. Caller must hold s.mu.
func (s *Sampler) budgetExhausted(now time.Time) bool {
	if now.Sub(s.budgetWindowStart) >= time.Minute {
		s.budgetWindowStart = now
		s.budgetUsed = 0
	}
	return s.cfg.BudgetKeepsPerMin > 0 && s.budgetUsed >= s.cfg.BudgetKeepsPerMin
}

// evictExpired drops decisions older than the TTL. Caller must hold s.mu.
func (s *Sampler) evictExpired(now time.Time) {
	keepFrom := 0
	for keepFrom < len(s.decOrder) {
		d := s.decided[s.decOrder[keepFrom]]
		if d == nil || now.Sub(d.DecidedAt) <= s.cfg.DecisionTTL {
			break
		}
		delete(s.decided, s.decOrder[keepFrom])
		keepFrom++
	}
	s.decOrder = s.decOrder[keepFrom:]
}

// GetDecision returns the final decision for a trace, if one exists.
func (s *Sampler) GetDecision(traceID string) (Decision, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	d, ok := s.decided[traceID]
	if !ok {
		return Decision{}, false
	}
	return *d, true
}

// ListDecisions returns decisions matching the given filters, newest first.
// keep/degraded/incomplete nil means "no filter". limit<=0 means no limit.
func (s *Sampler) ListDecisions(keep, degraded, incomplete *bool, limit int) []Decision {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []Decision
	for i := len(s.decOrder) - 1; i >= 0; i-- {
		d := s.decided[s.decOrder[i]]
		if d == nil {
			continue
		}
		if keep != nil && d.Keep != *keep {
			continue
		}
		if degraded != nil && d.Degraded != *degraded {
			continue
		}
		if incomplete != nil && d.Incomplete != *incomplete {
			continue
		}
		out = append(out, *d)
		if limit > 0 && len(out) >= limit {
			break
		}
	}
	return out
}

// GetTrace returns the spans of a kept trace.
func (s *Sampler) GetTrace(traceID string) ([]Span, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	spans, ok := s.kept[traceID]
	if !ok {
		return nil, false
	}
	return append([]Span(nil), spans...), true
}

// Stats returns a snapshot of sampler counters.
func (s *Sampler) Stats() Stats {
	s.mu.Lock()
	defer s.mu.Unlock()
	st := s.stats
	st.InflightTraces = len(s.inflight)
	st.BudgetUsed = s.budgetUsed
	return st
}

// PendingTraces reports which trace IDs are currently buffered (for tests
// and debugging).
func (s *Sampler) PendingTraces() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]string, 0, len(s.inflight))
	for id := range s.inflight {
		out = append(out, id)
	}
	sort.Strings(out)
	return out
}

func contains(xs []string, x string) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}
