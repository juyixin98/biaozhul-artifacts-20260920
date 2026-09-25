package tailsampling

import (
	"sort"
	"time"
)

// Finalization causes recorded in decisions/stats.
const (
	causeWait  = "wait_window_elapsed"
	causeTTL   = "max_ttl_exceeded"
	causeFlush = "forced_flush"
)

// Trace is the buffered, in-flight aggregation of spans sharing a trace id.
type Trace struct {
	TraceID string              `json:"trace_id"`
	Spans   []Span              `json:"spans"`
	spanIDs map[string]struct{} `json:"-"`

	FirstSeen time.Time `json:"first_seen"`
	LastSeen  time.Time `json:"last_seen"`
	StartMs   int64     `json:"start_ms"`
	EndMs     int64     `json:"end_ms"`

	RootSpanID string `json:"root_span_id,omitempty"`
	Complete   bool   `json:"complete"`

	WaitDeadline time.Time `json:"wait_deadline,omitempty"`
	TTLDeadline  time.Time `json:"ttl_deadline"`
	Finalized    bool      `json:"-"`

	waitTimer Timer `json:"-"`
	ttlTimer  Timer `json:"-"`
}

func newTrace(traceID string, now time.Time, ttl time.Duration) *Trace {
	return &Trace{
		TraceID:     traceID,
		spanIDs:     map[string]struct{}{},
		FirstSeen:   now,
		TTLDeadline: now.Add(ttl),
	}
}

func (t *Trace) addSpan(s Span, now time.Time) bool {
	if _, ok := t.spanIDs[s.SpanID]; ok {
		return false // duplicate span id: ignored
	}
	t.spanIDs[s.SpanID] = struct{}{}
	t.Spans = append(t.Spans, s)
	t.LastSeen = now
	if t.StartMs == 0 || s.StartTimeMs < t.StartMs {
		t.StartMs = s.StartTimeMs
	}
	if e := s.endMs(); e > t.EndMs {
		t.EndMs = e
	}
	if s.IsRoot() {
		t.Complete = true
		t.RootSpanID = s.SpanID
	}
	return true
}

func (t *Trace) errorCount() int {
	n := 0
	for _, s := range t.Spans {
		if s.isError() {
			n++
		}
	}
	return n
}

// SpanResult reports what happened to one ingested span.
type SpanResult struct {
	TraceID string `json:"trace_id"`
	SpanID  string `json:"span_id"`
	// Status is accepted | duplicate | late.
	Status string `json:"status"`
	Note   string `json:"note,omitempty"`
}

// IngestReport summarizes a batch ingest request.
type IngestReport struct {
	Received   int          `json:"received"`
	Accepted   int          `json:"accepted"`
	Duplicates int          `json:"duplicates"`
	Late       int          `json:"late_after_decision"`
	Results    []SpanResult `json:"results"`
}

// Clock abstracts time so the aggregator is deterministically testable.
type Clock interface {
	Now() time.Time
	AfterFunc(d time.Duration, f func()) Timer
	NewTicker(d time.Duration) Ticker
}

type Timer interface {
	Stop() bool
}

type Ticker interface {
	Stop()
	C() <-chan time.Time
}

type realTicker struct{ t *time.Ticker }

func (r realTicker) Stop()               { r.t.Stop() }
func (r realTicker) C() <-chan time.Time { return r.t.C }

type realClock struct{}

func (realClock) Now() time.Time                            { return time.Now() }
func (realClock) AfterFunc(d time.Duration, f func()) Timer { return time.AfterFunc(d, f) }
func (realClock) NewTicker(d time.Duration) Ticker          { return realTicker{time.NewTicker(d)} }

type finalizeMsg struct {
	traceID string
	cause   string
}

// Aggregator is the single-writer core. All state is owned by its loop
// goroutine; API methods communicate by channels. Timers also deliver
// finalization through the loop, so a trace's decision is made exactly once
// and can never change afterwards.
type Aggregator struct {
	cfg     Config
	clock   Clock
	store   Store
	sampler *Sampler

	ingestCh   chan ingestMsg
	finalizeCh chan finalizeMsg
	flushCh    chan chan []Decision
	getCh      chan getMsg
	listCh     chan listMsg
	statsCh    chan chan Stats
	snapshotCh chan chan struct{}
	idleCh     chan chan struct{}
	stopCh     chan struct{}
	doneCh     chan struct{}

	open      map[string]*Trace
	decisions map[string]*Decision
	late      map[string][]LateEvent

	keptByPolicy    map[string]int
	droppedByReason map[string]int
	kept, dropped   int
	incomplete      int
	downgrades      int
	overBudgetKeeps int
	lateCount       int
	decisionOrder   []string
}

type ingestMsg struct {
	spans []Span
	now   time.Time
	reply chan IngestReport
}

type getMsg struct {
	traceID string
	reply   chan *Decision
}

type listMsg struct {
	limit int
	reply chan []Decision
}

// NewAggregator wires the core and replays the persisted log/snapshot.
func NewAggregator(cfg Config, clock Clock, store Store) (*Aggregator, error) {
	if clock == nil {
		clock = realClock{}
	}
	budget := NewTokenBucket(cfg.BudgetCapacity, cfg.BudgetRefillPerSec, clock.Now())
	a := &Aggregator{
		cfg:             cfg,
		clock:           clock,
		store:           store,
		sampler:         NewSampler(cfg, budget),
		ingestCh:        make(chan ingestMsg),
		finalizeCh:      make(chan finalizeMsg, 256),
		flushCh:         make(chan chan []Decision),
		getCh:           make(chan getMsg),
		listCh:          make(chan listMsg),
		statsCh:         make(chan chan Stats),
		snapshotCh:      make(chan chan struct{}, 1),
		idleCh:          make(chan chan struct{}),
		stopCh:          make(chan struct{}),
		doneCh:          make(chan struct{}),
		open:            map[string]*Trace{},
		decisions:       map[string]*Decision{},
		late:            map[string][]LateEvent{},
		keptByPolicy:    map[string]int{},
		droppedByReason: map[string]int{},
	}
	if err := a.replay(); err != nil {
		return nil, err
	}
	go a.loop()
	return a, nil
}

func (a *Aggregator) replay() error {
	decs, err := a.store.LoadDecisions()
	if err != nil {
		return err
	}
	seen := map[string]struct{}{}
	for i := range decs {
		d := decs[i]
		if _, dup := seen[d.TraceID]; dup {
			// A trace is finalized at most once; a duplicated log line must
			// not double-count counters. Last write wins for the stored value.
			a.decisions[d.TraceID] = &d
			continue
		}
		seen[d.TraceID] = struct{}{}
		a.decisions[d.TraceID] = &d
		a.decisionOrder = append(a.decisionOrder, d.TraceID)
		if d.Kept {
			a.kept++
			a.keptByPolicy[d.Policy]++
		} else {
			a.dropped++
			a.droppedByReason[d.ReasonCode]++
		}
		if !d.Complete {
			a.incomplete++
		}
		a.countDegraded(d)
	}
	lateEvents, err := a.store.LoadLate()
	if err != nil {
		return err
	}
	for _, e := range lateEvents {
		a.late[e.TraceID] = append(a.late[e.TraceID], e)
		if d, ok := a.decisions[e.TraceID]; ok {
			d.LateSpans = append(d.LateSpans, e)
		}
	}
	a.lateCount = len(lateEvents)

	snap, ok, err := a.store.ReadSnapshot()
	if err != nil {
		return err
	}
	if ok {
		now := a.clock.Now()
		for i := range snap.Open {
			tr := snap.Open[i]
			if _, decided := a.decisions[tr.TraceID]; decided {
				continue // decided between snapshot and crash; log wins
			}
			tr.spanIDs = map[string]struct{}{}
			for _, s := range tr.Spans {
				tr.spanIDs[s.SpanID] = struct{}{}
			}
			tr.Finalized = false
			a.open[tr.TraceID] = &tr
			a.scheduleLoaded(&tr, now)
		}
	}
	return nil
}

// scheduleLoaded arms wait/ttl timers with the remaining time at restart.
// Remaining time is computed against the injected clock (never time.Until,
// which ignores a fake test clock and would fire everything immediately).
func (a *Aggregator) scheduleLoaded(tr *Trace, now time.Time) {
	if !tr.WaitDeadline.IsZero() {
		if d := tr.WaitDeadline.Sub(now); d > 0 {
			traceID := tr.TraceID
			tr.waitTimer = a.clock.AfterFunc(d, func() { a.enqueueFinalize(traceID, causeWait) })
		} else {
			a.enqueueFinalize(tr.TraceID, causeWait)
		}
	}
	if d := tr.TTLDeadline.Sub(now); d > 0 {
		traceID := tr.TraceID
		tr.ttlTimer = a.clock.AfterFunc(d, func() { a.enqueueFinalize(traceID, causeTTL) })
	} else {
		a.enqueueFinalize(tr.TraceID, causeTTL)
	}
}

func (a *Aggregator) enqueueFinalize(traceID, cause string) {
	select {
	case a.finalizeCh <- finalizeMsg{traceID: traceID, cause: cause}:
	case <-a.stopCh:
	}
}

func (a *Aggregator) loop() {
	defer close(a.doneCh)
	var ticker Ticker
	var tickCh <-chan time.Time
	if a.cfg.SnapshotInterval > 0 {
		ticker = a.clock.NewTicker(a.cfg.SnapshotInterval)
		defer ticker.Stop()
		tickCh = ticker.C()
	}
	for {
		select {
		case m := <-a.ingestCh:
			m.reply <- a.handleIngest(m)
		case m := <-a.finalizeCh:
			if tr, ok := a.open[m.traceID]; ok {
				a.finalize(tr, m.cause)
			}
		case reply := <-a.flushCh:
			reply <- a.flushAll()
		case m := <-a.getCh:
			m.reply <- a.decisions[m.traceID]
		case m := <-a.listCh:
			m.reply <- a.listDecisions(m.limit)
		case reply := <-a.statsCh:
			reply <- a.stats()
		case reply := <-a.snapshotCh:
			a.writeSnapshot()
			close(reply)
		case <-tickCh:
			a.writeSnapshot()
		case reply := <-a.idleCh:
			// Every message queued before this barrier has now been
			// processed (channels are FIFO; timer callbacks enqueue
			// finalize messages, so advancing the clock then waiting here
			// guarantees those finalizations are complete).
			close(reply)
		case <-a.stopCh:
			a.writeSnapshot()
			return
		}
	}
}

func (a *Aggregator) handleIngest(m ingestMsg) IngestReport {
	report := IngestReport{Received: len(m.spans), Results: make([]SpanResult, 0, len(m.spans))}
	for _, s := range m.spans {
		// Already decided: immutable decision; record the late arrival.
		if d, decided := a.decisions[s.TraceID]; decided {
			e := LateEvent{
				TraceID:     s.TraceID,
				SpanID:      s.SpanID,
				Name:        s.Name,
				Status:      s.Status,
				ArrivedAtMs: m.now.UnixMilli(),
				DecidedAtMs: d.DecidedAtMs,
				Note:        "span arrived after final decision; decision is immutable",
			}
			if err := a.store.AppendLate(e); err == nil {
				a.late[s.TraceID] = append(a.late[s.TraceID], e)
				d.LateSpans = append(d.LateSpans, e)
				a.lateCount++
			}
			report.Late++
			report.Results = append(report.Results, SpanResult{
				TraceID: s.TraceID, SpanID: s.SpanID, Status: "late", Note: e.Note,
			})
			continue
		}

		tr := a.open[s.TraceID]
		if tr == nil {
			tr = newTrace(s.TraceID, m.now, a.cfg.MaxTTL)
			a.open[s.TraceID] = tr
			traceID := tr.TraceID
			tr.ttlTimer = a.clock.AfterFunc(a.cfg.MaxTTL, func() {
				a.enqueueFinalize(traceID, causeTTL)
			})
		}
		if !tr.addSpan(s, m.now) {
			report.Duplicates++
			report.Results = append(report.Results, SpanResult{
				TraceID: s.TraceID, SpanID: s.SpanID, Status: "duplicate",
				Note: "span_id already seen for this trace; ignored",
			})
			continue
		}
		report.Accepted++
		report.Results = append(report.Results, SpanResult{
			TraceID: s.TraceID, SpanID: s.SpanID, Status: "accepted",
		})

		// Trace became complete: arm the decision wait window once.
		// Spans that arrive during the window (including late error spans)
		// are part of the evaluation.
		if tr.Complete && tr.waitTimer == nil {
			tr.WaitDeadline = m.now.Add(a.cfg.WaitWindow)
			traceID := tr.TraceID
			tr.waitTimer = a.clock.AfterFunc(a.cfg.WaitWindow, func() {
				a.enqueueFinalize(traceID, causeWait)
			})
		}
	}
	return report
}

func (a *Aggregator) finalize(tr *Trace, cause string) {
	if tr.Finalized {
		return
	}
	tr.Finalized = true
	if tr.waitTimer != nil {
		tr.waitTimer.Stop()
	}
	if tr.ttlTimer != nil {
		tr.ttlTimer.Stop()
	}
	delete(a.open, tr.TraceID)

	now := a.clock.Now()
	in := traceInput{
		traceID:    tr.TraceID,
		spanCount:  len(tr.Spans),
		errorCount: tr.errorCount(),
		durationMs: tr.EndMs - tr.StartMs,
		startMs:    tr.StartMs,
		endMs:      tr.EndMs,
		complete:   tr.Complete,
		rootSpanID: tr.RootSpanID,
		now:        now,
	}
	d, _ := a.sampler.Evaluate(in)
	d.DecidedAtMs = now.UnixMilli()

	switch cause {
	case causeWait:
		d.Reason += " | finalized after decision wait window"
	case causeTTL:
		if !tr.Complete {
			d.Complete = false
			d.ReasonCode = ReasonForcedIncomplete
			d.Reason = "trace not observed as complete before max TTL; marked INCOMPLETE and finalized with partial data | provisional policy: " + d.Reason
		} else {
			d.Reason += " | finalized at max TTL (still receiving spans)"
		}
	case causeFlush:
		if !tr.Complete {
			d.Complete = false
			d.ReasonCode = ReasonForcedIncomplete
			d.Reason = "forced flush before trace completion; marked INCOMPLETE | provisional policy: " + d.Reason
		} else {
			d.Reason += " | finalized by forced flush"
		}
	}

	// Attach any late events (normally none yet; keeps replay uniform).
	d.LateSpans = append(d.LateSpans, a.late[tr.TraceID]...)

	if err := a.store.AppendDecision(d); err != nil {
		// Persistence failure must not vanish: surface via panic-free path.
		// The decision stays queryable in memory and will be retried via the
		// next snapshot only if open; log file loss is reported in reason.
		d.Reason += " | WARNING: decision persistence failed: " + err.Error()
	}
	a.recordDecision(d)
}

func (a *Aggregator) recordDecision(d Decision) {
	stored := d
	a.decisions[d.TraceID] = &stored
	a.decisionOrder = append(a.decisionOrder, d.TraceID)
	if d.Kept {
		a.kept++
		a.keptByPolicy[d.Policy]++
	} else {
		a.dropped++
		a.droppedByReason[d.ReasonCode]++
	}
	if !d.Complete {
		a.incomplete++
	}
	a.countDegraded(d)
}

// countDegraded partitions degraded decisions into KEEP->DROP downgrades and
// error-priority keeps that exceeded the exhausted budget.
func (a *Aggregator) countDegraded(d Decision) {
	if !d.Degraded {
		return
	}
	if d.Kept {
		a.overBudgetKeeps++
	} else {
		a.downgrades++
	}
}

func (a *Aggregator) flushAll() []Decision {
	ids := make([]string, 0, len(a.open))
	for id := range a.open {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	out := make([]Decision, 0, len(ids))
	for _, id := range ids {
		tr := a.open[id]
		a.finalize(tr, causeFlush)
		if d := a.decisions[id]; d != nil {
			out = append(out, *d)
		}
	}
	return out
}

func (a *Aggregator) listDecisions(limit int) []Decision {
	n := len(a.decisionOrder)
	if limit <= 0 || limit > n {
		limit = n
	}
	out := make([]Decision, 0, limit)
	for i := n - 1; i >= n-limit; i-- {
		if d := a.decisions[a.decisionOrder[i]]; d != nil {
			out = append(out, *d)
		}
	}
	return out
}

func (a *Aggregator) stats() Stats {
	return Stats{
		GeneratedAtMs:           a.clock.Now().UnixMilli(),
		OpenTraces:              len(a.open),
		TotalDecided:            a.kept + a.dropped,
		Kept:                    a.kept,
		Dropped:                 a.dropped,
		KeptByPolicy:            copyCounts(a.keptByPolicy),
		DroppedByReason:         copyCounts(a.droppedByReason),
		Incomplete:              a.incomplete,
		DegradedDowngrades:      a.downgrades,
		DegradedOverBudgetKeeps: a.overBudgetKeeps,
		LateArrivals:            a.lateCount,
		BudgetObservations:      a.sampler.budget.view(a.clock.Now()),
	}
}

func copyCounts(m map[string]int) map[string]int {
	out := make(map[string]int, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}

func (a *Aggregator) writeSnapshot() {
	trs := make([]Trace, 0, len(a.open))
	for _, tr := range a.open {
		trs = append(trs, *tr)
	}
	_ = a.store.WriteSnapshot(Snapshot{TakenAtMs: a.clock.Now().UnixMilli(), Open: trs})
}

// ---- public synchronous API (used by HTTP handlers and tests) ----

// Ingest adds spans and returns the per-span report.
func (a *Aggregator) Ingest(spans []Span) IngestReport {
	reply := make(chan IngestReport, 1)
	a.ingestCh <- ingestMsg{spans: spans, now: a.clock.Now(), reply: reply}
	return <-reply
}

// Flush immediately finalizes all open traces.
func (a *Aggregator) Flush() []Decision {
	reply := make(chan []Decision, 1)
	a.flushCh <- reply
	return <-reply
}

// Decision returns the immutable decision for a trace, or nil.
func (a *Aggregator) Decision(traceID string) *Decision {
	reply := make(chan *Decision, 1)
	a.getCh <- getMsg{traceID: traceID, reply: reply}
	return <-reply
}

// ListDecisions returns up to limit most recent decisions (0 = all).
func (a *Aggregator) ListDecisions(limit int) []Decision {
	reply := make(chan []Decision, 1)
	a.listCh <- listMsg{limit: limit, reply: reply}
	return <-reply
}

// Stats returns a counters snapshot.
func (a *Aggregator) Stats() Stats {
	reply := make(chan Stats, 1)
	a.statsCh <- reply
	return <-reply
}

// SnapshotNow triggers an out-of-schedule open-trace snapshot and waits
// until it has been written.
func (a *Aggregator) SnapshotNow() {
	reply := make(chan struct{})
	a.snapshotCh <- reply
	<-reply
}

// WaitIdle blocks until every message already queued (including timer-driven
// finalizations enqueued by a fake clock advance) has been processed.
func (a *Aggregator) WaitIdle() {
	reply := make(chan struct{})
	a.idleCh <- reply
	<-reply
}

// Close stops the loop after a final snapshot. Safe to call once.
func (a *Aggregator) Close() {
	close(a.stopCh)
	<-a.doneCh
}

// compile-time guard: realClock satisfies Clock.
var _ Clock = realClock{}
