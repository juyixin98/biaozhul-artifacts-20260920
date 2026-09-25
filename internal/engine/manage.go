package engine

import (
	"fmt"
	"sort"
	"time"
)

// IngestItem is one sample in a batch request. TS may be zero, in which case
// the current virtual clock time is used.
type IngestItem struct {
	Metric string
	TS     time.Time
	Value  float64
}

// IngestResult reports what happened to one submitted sample.
type IngestResult struct {
	Metric    string    `json:"metric"`
	TS        time.Time `json:"ts"`
	Value     float64   `json:"value"`
	Accepted  bool      `json:"accepted"`  // advanced the clock and was evaluated
	Late      bool      `json:"late"`      // timestamp before the virtual clock: stored, not evaluated
	Duplicate bool      `json:"duplicate"` // same metric+ts as a previously stored sample
	Overwrote bool      `json:"overwrote"` // duplicate replaced a different value
	Events    []Event   `json:"events,omitempty"`
}

// CreateRule validates and installs a new rule.
func (e *Engine) CreateRule(r Rule) error {
	if err := r.Validate(); err != nil {
		return err
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if _, exists := e.rules[r.ID]; exists {
		return fmt.Errorf("rule %q already exists", r.ID)
	}
	r.applyDefaults(e.now)
	if err := r.Validate(); err != nil {
		return err
	}
	rc := r
	e.rules[r.ID] = &rc
	e.states[r.ID] = newRuntimeState(e.now)
	e.persistLocked()
	return nil
}

func newRuntimeState(now time.Time) *RuntimeState {
	return &RuntimeState{State: StateInactive, EnteredAt: now, UpdatedAt: now}
}

// UpdateRule replaces the configuration of an existing rule. The version is
// bumped and the runtime state is reset unconditionally — callers get an
// explicit rule_reset audit event, never a silent carry-over.
func (e *Engine) UpdateRule(r Rule) error {
	if err := r.Validate(); err != nil {
		return err
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	old, ok := e.rules[r.ID]
	if !ok {
		return fmt.Errorf("rule %q not found", r.ID)
	}
	r.applyDefaults(old.CreatedAt)
	if err := r.Validate(); err != nil {
		return err
	}
	r.CreatedAt = old.CreatedAt
	r.Version = old.Version + 1
	r.UpdatedAt = e.now
	rc := r
	e.rules[r.ID] = &rc
	e.states[r.ID] = newRuntimeState(e.now)

	ev := Event{
		Seq:     e.nextSeq,
		RuleID:  r.ID,
		Metric:  r.Metric,
		Type:    EventRuleReset,
		TS:      e.now,
		From:    StateInactive,
		To:      StateInactive,
		Message: fmt.Sprintf("rule configuration updated to version %d, runtime state reset", r.Version),
		Version: r.Version,
	}
	e.nextSeq++
	e.events = append(e.events, ev)
	if len(e.events) > maxEvents {
		e.events = e.events[len(e.events)-maxEvents:]
	}
	e.persistLocked()
	return nil
}

// DeleteRule removes a rule and its runtime state. Stored samples for the
// metric are kept (they are raw observations and may serve another rule).
func (e *Engine) DeleteRule(id string) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	if _, ok := e.rules[id]; !ok {
		return fmt.Errorf("rule %q not found", id)
	}
	delete(e.rules, id)
	delete(e.states, id)
	e.persistLocked()
	return nil
}

// GetRule returns a deep value copy of rule + state.
func (e *Engine) GetRule(id string) (RuleSnapshot, bool) {
	e.mu.Lock()
	defer e.mu.Unlock()
	r, ok := e.rules[id]
	if !ok {
		return RuleSnapshot{}, false
	}
	st := e.states[id]
	return RuleSnapshot{Rule: *r, State: *st.clone()}, true
}

// ListRules returns all rule snapshots ordered by ID.
func (e *Engine) ListRules() []RuleSnapshot {
	e.mu.Lock()
	defer e.mu.Unlock()
	ids := make([]string, 0, len(e.rules))
	for id := range e.rules {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	out := make([]RuleSnapshot, 0, len(ids))
	for _, id := range ids {
		out = append(out, RuleSnapshot{Rule: *e.rules[id], State: *e.states[id].clone()})
	}
	return out
}

// Ingest accepts a batch of samples. The virtual clock advances only
// forward, driven by accepted sample timestamps:
//
//   - ts > clock: accepted; clock moves to ts; the sample is stored and all
//     rules for its metric are evaluated at ts.
//   - ts == clock (duplicate timestamp): the value overwrites the stored
//     one and rules ARE evaluated at the same instant; because no virtual
//     time elapsed, streak durations do not grow ("repeated samples do not
//     accumulate duration").
//   - ts < clock (late/out-of-order): stored for query completeness and
//     marked late; it never re-drives the state machine and produces no
//     notifications.
//
// Duplicate detection is metric + timestamp identity.
func (e *Engine) Ingest(items []IngestItem) []IngestResult {
	e.mu.Lock()
	defer e.mu.Unlock()

	// De-duplicate within the batch (last write wins) and sort ascending so
	// a batch like [t3, t1, t2] behaves exactly like [t1, t2, t3].
	uniq := map[string]IngestItem{}
	order := []string{}
	// Sample coordinates in this synthetic system are second-precision;
	// normalizing also avoids sub-nanosecond map-key mismatches between
	// independently parsed equal timestamps.
	for _, it := range items {
		ts := it.TS
		if ts.IsZero() {
			ts = e.now
		}
		it.TS = normTS(ts)
		key := it.Metric + "|" + it.TS.String()
		if _, ok := uniq[key]; !ok {
			order = append(order, key)
		}
		uniq[key] = it
	}
	sort.Slice(order, func(i, j int) bool { return uniq[order[i]].TS.Before(uniq[order[j]].TS) })

	results := make([]IngestResult, 0, len(order))
	for _, key := range order {
		it := uniq[key]
		res := IngestResult{Metric: it.Metric, TS: it.TS, Value: it.Value}

		if it.TS.Before(e.now) {
			// Late sample: store, mark, never evaluate.
			dup, overwrote := e.storeSampleLocked(it.Metric, it.TS, it.Value)
			res.Late = true
			res.Duplicate = dup
			res.Overwrote = overwrote
			results = append(results, res)
			continue
		}

		if it.TS.After(e.now) {
			e.now = it.TS
		}
		dup, overwrote := e.storeSampleLocked(it.Metric, it.TS, it.Value)
		res.Duplicate = dup
		res.Overwrote = overwrote
		res.Accepted = true

		var evs []Event
		for _, rule := range e.rulesForMetricLocked(it.Metric) {
			v := it.Value
			evs = append(evs, e.evaluateLocked(rule.ID, it.TS, &v)...)
		}
		res.Events = cloneEvents(evs)
		results = append(results, res)
	}
	e.persistLocked()
	return results
}

// rulesForMetricLocked returns rules for a metric ordered by ID.
func (e *Engine) rulesForMetricLocked(metric string) []*Rule {
	var out []*Rule
	for _, r := range e.rules {
		if r.Metric == metric {
			out = append(out, r)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

// storeSampleLocked inserts/overwrites one value and reports whether the
// timestamp already existed (dup) and whether a different value was replaced
// (overwrote). It retains the newest maxSamplesPerMetric.
func (e *Engine) storeSampleLocked(metric string, ts time.Time, value float64) (dup, overwrote bool) {
	ts = normTS(ts)
	m := e.samples[metric]
	if m == nil {
		m = map[time.Time]float64{}
		e.samples[metric] = m
		e.tsIndex[metric] = nil
	}
	if old, exists := m[ts]; exists {
		return true, old != value
	}
	e.tsIndex[metric] = append(e.tsIndex[metric], ts)
	sort.Slice(e.tsIndex[metric], func(i, j int) bool {
		return e.tsIndex[metric][i].Before(e.tsIndex[metric][j])
	})
	m[ts] = value

	if len(e.tsIndex[metric]) > maxSamplesPerMetric {
		drop := e.tsIndex[metric][:len(e.tsIndex[metric])-maxSamplesPerMetric]
		for _, t := range drop {
			delete(m, t)
		}
		e.tsIndex[metric] = e.tsIndex[metric][len(drop):]
	}
	return false, false
}

// QueryEvents returns events ordered by sequence. Filters are optional:
// empty ruleID means all rules; notifOnly hides rule_reset audits; since
// filters on virtual timestamp.
func (e *Engine) QueryEvents(ruleID string, notifOnly bool, since *time.Time) []Event {
	e.mu.Lock()
	defer e.mu.Unlock()
	out := []Event{}
	for _, ev := range e.events {
		if ruleID != "" && ev.RuleID != ruleID {
			continue
		}
		if notifOnly && !ev.Type.IsNotification() {
			continue
		}
		if since != nil && ev.TS.Before(*since) {
			continue
		}
		out = append(out, ev)
	}
	return cloneEvents(out)
}

// QuerySamples returns stored samples for a metric in [from, to], newest
// first, capped at limit (<=0 means the engine default cap).
func (e *Engine) QuerySamples(metric string, from, to *time.Time, limit int) []Sample {
	e.mu.Lock()
	defer e.mu.Unlock()
	if limit <= 0 || limit > maxSamplesPerMetric {
		limit = maxSamplesPerMetric
	}
	m := e.samples[metric]
	if m == nil {
		return []Sample{}
	}
	idx := e.tsIndex[metric]
	out := []Sample{}
	for i := len(idx) - 1; i >= 0; i-- {
		ts := idx[i]
		if from != nil && ts.Before(*from) {
			continue
		}
		if to != nil && ts.After(*to) {
			continue
		}
		out = append(out, Sample{Metric: metric, TS: ts, Value: m[ts]})
		if len(out) >= limit {
			break
		}
	}
	return out
}

// ---------- snapshot / restore ----------

func (e *Engine) persistLocked() {
	if e.persist == nil {
		return
	}
	e.persist(e.exportLocked())
}

// Export returns a deep value copy snapshot of the whole engine.
func (e *Engine) Export() *Snapshot {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.exportLocked()
}

func (e *Engine) exportLocked() *Snapshot {
	snap := &Snapshot{
		ClockNow: e.now,
		Rules:    make([]Rule, 0, len(e.rules)),
		States:   map[string]RuntimeState{},
		Samples:  map[string]map[time.Time]float64{},
		Events:   cloneEvents(e.events),
		NextSeq:  e.nextSeq,
		Version:  1,
	}
	ids := make([]string, 0, len(e.rules))
	for id := range e.rules {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		snap.Rules = append(snap.Rules, *e.rules[id])
		snap.States[id] = *e.states[id].clone()
	}
	for metric, m := range e.samples {
		cp := make(map[time.Time]float64, len(m))
		for ts, v := range m {
			cp[ts] = v
		}
		snap.Samples[metric] = cp
	}
	return snap
}

// Restore replaces all engine state with snap (validated). Used at startup.
func (e *Engine) Restore(snap *Snapshot) error {
	if snap == nil {
		return fmt.Errorf("nil snapshot")
	}
	e.mu.Lock()
	defer e.mu.Unlock()

	rules := map[string]*Rule{}
	states := map[string]*RuntimeState{}
	for i := range snap.Rules {
		r := snap.Rules[i]
		if err := r.Validate(); err != nil {
			return fmt.Errorf("snapshot rule %q invalid: %w", r.ID, err)
		}
		if r.Version == 0 {
			r.Version = 1
		}
		rc := r
		rules[r.ID] = &rc
		st, ok := snap.States[r.ID]
		if !ok || !validState(st.State) {
			st = *newRuntimeState(r.CreatedAt)
		}
		sc := st
		states[r.ID] = &sc
	}

	samples := map[string]map[time.Time]float64{}
	tsIndex := map[string][]time.Time{}
	for metric, m := range snap.Samples {
		cp := make(map[time.Time]float64, len(m))
		idx := make([]time.Time, 0, len(m))
		for ts, v := range m {
			cp[ts] = v
			idx = append(idx, ts)
		}
		sort.Slice(idx, func(i, j int) bool { return idx[i].Before(idx[j]) })
		if len(idx) > maxSamplesPerMetric {
			idx = idx[len(idx)-maxSamplesPerMetric:]
			cp2 := make(map[time.Time]float64, len(idx))
			for _, ts := range idx {
				cp2[ts] = cp[ts]
			}
			cp = cp2
		}
		samples[metric] = cp
		tsIndex[metric] = idx
	}

	events := cloneEvents(snap.Events)
	if len(events) > maxEvents {
		events = events[len(events)-maxEvents:]
	}

	e.now = snap.ClockNow
	if e.now.IsZero() {
		e.now = Epoch
	}
	e.rules = rules
	e.states = states
	e.samples = samples
	e.tsIndex = tsIndex
	e.events = events
	if snap.NextSeq > 0 {
		e.nextSeq = snap.NextSeq
	}
	return nil
}

// ---------- helpers ----------

func cloneEvents(in []Event) []Event {
	out := make([]Event, len(in))
	for i := range in {
		ev := in[i]
		if in[i].Value != nil {
			v := *in[i].Value
			ev.Value = &v
		}
		out[i] = ev
	}
	return out
}

func (s *RuntimeState) clone() *RuntimeState {
	cp := *s
	if s.HotSince != nil {
		v := *s.HotSince
		cp.HotSince = &v
	}
	if s.ColdSince != nil {
		v := *s.ColdSince
		cp.ColdSince = &v
	}
	if s.LastSampleTS != nil {
		v := *s.LastSampleTS
		cp.LastSampleTS = &v
	}
	if s.LastValue != nil {
		v := *s.LastValue
		cp.LastValue = &v
	}
	return &cp
}

// normTS truncates sample timestamps to second precision in UTC.
func normTS(t time.Time) time.Time {
	return t.UTC().Truncate(time.Second)
}
