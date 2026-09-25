package engine

import (
	"fmt"
	"sort"
	"sync"
	"time"
)

// Epoch is the starting point of the virtual clock for a fresh engine. A
// fixed epoch (rather than wall-clock time) keeps every demo reproducible.
var Epoch = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

const (
	maxSamplesPerMetric = 1000
	maxEvents           = 2000
)

// Snapshot is the complete serializable engine state, used by the file
// persistence layer and for restore-after-restart.
type Snapshot struct {
	ClockNow time.Time                        `json:"clock_now"`
	Rules    []Rule                           `json:"rules"`
	States   map[string]RuntimeState          `json:"states"`
	Samples  map[string]map[time.Time]float64 `json:"samples"`
	Events   []Event                          `json:"events"`
	NextSeq  int64                            `json:"next_seq"`
	Version  int                              `json:"snapshot_version"`
}

// Engine is the virtual-clock-driven alert evaluation core. It is safe for
// concurrent use.
type Engine struct {
	mu sync.Mutex

	now time.Time

	rules  map[string]*Rule
	states map[string]*RuntimeState

	// samples[metric][ts] = value. tsIndex keeps timestamps ordered so the
	// newest N can be retained cheaply and queries return sorted output.
	samples map[string]map[time.Time]float64
	tsIndex map[string][]time.Time

	events  []Event
	nextSeq int64

	persist func(*Snapshot)
}

// NewEngine creates an empty engine with the virtual clock at Epoch.
func NewEngine() *Engine {
	return &Engine{
		now:     Epoch,
		rules:   map[string]*Rule{},
		states:  map[string]*RuntimeState{},
		samples: map[string]map[time.Time]float64{},
		tsIndex: map[string][]time.Time{},
		events:  []Event{},
		nextSeq: 1,
	}
}

// WithPersist installs a callback invoked after every state-changing
// operation (rule CRUD, ingest, tick). It must be called before the engine
// is used.
func (e *Engine) WithPersist(fn func(*Snapshot)) *Engine {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.persist = fn
	return e
}

// Now returns the current virtual time.
func (e *Engine) Now() time.Time {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.now
}

// Tick advances the virtual clock by d (d must be positive) and re-evaluates
// every rule. It is how no-data detection is driven between samples. All
// transition events produced by the tick are returned.
func (e *Engine) Tick(d time.Duration) ([]Event, error) {
	if d <= 0 {
		return nil, fmt.Errorf("tick duration must be positive, got %s", d)
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	e.now = e.now.Add(d)
	evs := e.evaluateAllLocked()
	e.persistLocked()
	return evs, nil
}

// TickTo advances (never retreats) the virtual clock to t and evaluates.
func (e *Engine) TickTo(t time.Time) ([]Event, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if t.Before(e.now) {
		return nil, fmt.Errorf("cannot move virtual clock backwards: now %s, requested %s", e.now.UTC().Format(time.RFC3339Nano), t.UTC().Format(time.RFC3339Nano))
	}
	if t.Equal(e.now) {
		return nil, nil
	}
	e.now = t
	evs := e.evaluateAllLocked()
	e.persistLocked()
	return evs, nil
}

// evaluateAllLocked runs the staleness/state evaluation for every rule in
// deterministic ID order.
func (e *Engine) evaluateAllLocked() []Event {
	ids := make([]string, 0, len(e.rules))
	for id := range e.rules {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	var out []Event
	for _, id := range ids {
		out = append(out, e.evaluateLocked(id, e.now, nil)...)
	}
	return out
}

// evaluateLocked evaluates one rule at virtual time at. When sampleVal is
// non-nil, a fresh sample at exactly `at` is being applied; otherwise the
// rule is evaluated against its latest stored sample (tick path).
func (e *Engine) evaluateLocked(id string, at time.Time, sampleVal *float64) []Event {
	rule := e.rules[id]
	st := e.states[id]
	if rule == nil || st == nil {
		return nil
	}

	// Fresh sample path: the sample itself defines "last seen".
	if sampleVal != nil {
		v := *sampleVal
		ts := at
		st.LastSampleTS = &ts
		st.LastValue = &v
		return e.handleFreshLocked(rule, st, at, v)
	}

	// Tick path: staleness is measured from the newest sample, or from rule
	// creation if no sample ever arrived.
	anchor := rule.CreatedAt
	if st.LastSampleTS != nil {
		anchor = *st.LastSampleTS
	}
	if at.Sub(anchor) >= rule.NoDataFor.Duration {
		if st.State != StateNodata {
			msg := fmt.Sprintf("no data for %s (last contact %s)", rule.NoDataFor.Duration, anchor.UTC().Format(time.RFC3339Nano))
			ev := e.enterLocked(rule, st, StateNodata, at, msg, st.LastValue, EventNodata)
			return []Event{*ev}
		}
		return nil
	}

	// Data still fresh enough; classify the latest value.
	if st.LastValue == nil {
		return nil // rule created within the no-data window, no sample yet
	}
	return e.handleFreshLocked(rule, st, at, *st.LastValue)
}

// handleFreshLocked applies a fresh classification at time at, returning the
// transition events (zero, one, or two — nodata resume can chain).
func (e *Engine) handleFreshLocked(rule *Rule, st *RuntimeState, at time.Time, value float64) []Event {
	t := rule.classify(value)

	// A nodata rule receiving a sample first emits data_resumed, landing in
	// the natural state for the value, then immediately applies the regular
	// transition logic (so trigger_for=0 can chain straight into firing).
	if st.State == StateNodata {
		var evs []Event
		if t == tierHot {
			hs := at
			st.HotSince = &hs
			st.ColdSince = nil
			ev := e.enterLocked(rule, st, StatePending, at,
				fmt.Sprintf("data resumed with value %g", value), &value, EventDataResumed)
			evs = append(evs, *ev)
		} else {
			cs := at
			st.ColdSince = &cs
			st.HotSince = nil
			ev := e.enterLocked(rule, st, StateInactive, at,
				fmt.Sprintf("data resumed with value %g", value), &value, EventDataResumed)
			evs = append(evs, *ev)
		}
		evs = append(evs, e.handleFreshLocked(rule, st, at, value)...)
		return evs
	}

	switch st.State {
	case StateInactive:
		if t == tierHot {
			hs := at
			st.HotSince = &hs
			st.ColdSince = nil
			e.enterLocked(rule, st, StatePending, at,
				fmt.Sprintf("value %g breaches %s %g, pending for %s", value, rule.Operator, rule.Threshold, rule.TriggerFor.Duration), &value, "")
			// trigger_for=0 means immediate: chain straight into firing so
			// the first hot sample both enters pending and notifies.
			if rule.TriggerFor.Duration <= 0 {
				return e.handleFreshLocked(rule, st, at, value)
			}
		}
		// warm/cold while inactive: nothing to do.

	case StatePending:
		switch t {
		case tierHot:
			if st.HotSince == nil {
				hs := at
				st.HotSince = &hs
			}
			if at.Sub(*st.HotSince) >= rule.TriggerFor.Duration {
				msg := fmt.Sprintf("alert firing: value %g %s %g sustained for %s", value, rule.Operator, rule.Threshold, rule.TriggerFor.Duration)
				ev := e.enterLocked(rule, st, StateFiring, at, msg, &value, EventFiring)
				return []Event{*ev}
			}
		default:
			// warm or cold before the trigger duration elapses: condition
			// not sustained, drop back immediately.
			st.HotSince = nil
			e.enterLocked(rule, st, StateInactive, at,
				fmt.Sprintf("breach not sustained, back to inactive (value %g)", value), &value, "")
		}

	case StateFiring:
		switch t {
		case tierHot:
			st.ColdSince = nil // still breaching, recovery streak stays reset
		case tierWarm:
			// Hysteresis band: alert stays open, recovery streak resets.
			st.ColdSince = nil
		default: // cold
			if rule.RecoverFor.Duration <= 0 {
				st.HotSince = nil
				st.ColdSince = nil
				ev := e.enterLocked(rule, st, StateInactive, at,
					fmt.Sprintf("alert resolved: value %g back to normal", value), &value, EventResolved)
				return []Event{*ev}
			}
			cs := at
			st.ColdSince = &cs
			e.enterLocked(rule, st, StateRecovering, at,
				fmt.Sprintf("value %g no longer satisfies %s %g, recovering for %s", value, rule.Operator, rule.Threshold, rule.RecoverFor.Duration), &value, "")
		}

	case StateRecovering:
		switch t {
		case tierHot:
			// Re-breach before recovery: straight back to firing. No new
			// notification is emitted — the alert never resolved.
			hs := at
			st.HotSince = &hs
			st.ColdSince = nil
			e.enterLocked(rule, st, StateFiring, at,
				fmt.Sprintf("value %g re-breaches %s %g before recovery", value, rule.Operator, rule.Threshold), &value, "")
		case tierWarm:
			// Band holds the alert open; restart the recovery countdown.
			st.ColdSince = nil
		default: // cold
			if st.ColdSince == nil {
				cs := at
				st.ColdSince = &cs
			}
			if at.Sub(*st.ColdSince) >= rule.RecoverFor.Duration {
				st.HotSince = nil
				st.ColdSince = nil
				ev := e.enterLocked(rule, st, StateInactive, at,
					fmt.Sprintf("alert resolved: value %g normal for %s", value, rule.RecoverFor.Duration), &value, EventResolved)
				return []Event{*ev}
			}
		}
	}
	return nil
}

// enterLocked moves a rule to state s, stamping timestamps and, when et is
// non-empty, recording a notification/audit event. The event pointer is also
// returned (nil when et is empty).
func (e *Engine) enterLocked(rule *Rule, st *RuntimeState, s State, at time.Time, msg string, value *float64, et EventType) *Event {
	from := st.State
	changed := from != s
	st.State = s
	if changed {
		st.EnteredAt = at
		st.UpdatedAt = at
	}
	if et == "" {
		return nil
	}
	ev := Event{
		Seq:     e.nextSeq,
		RuleID:  rule.ID,
		Metric:  rule.Metric,
		Type:    et,
		TS:      at,
		From:    from,
		To:      s,
		Message: msg,
		Value:   value,
		Version: rule.Version,
	}
	e.nextSeq++
	e.events = append(e.events, ev)
	if len(e.events) > maxEvents {
		e.events = e.events[len(e.events)-maxEvents:]
	}
	return &e.events[len(e.events)-1]
}
