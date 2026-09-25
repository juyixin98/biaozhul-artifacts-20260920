// Package engine implements the alert hysteresis state machine.
//
// States per rule:
//
//	ok ──breach sample──────────────► pending
//	ok ──(no data timeout)──────────► no_data
//	pending ──good sample──────────► ok
//	pending ──(breach held ≥ pending_for)──► alerting   [event: firing]
//	pending ──(no data timeout)─────► no_data  [event: no_data_start]
//	alerting ──good sample─────────► recovering
//	alerting ──(no data timeout)────► no_data  [event: no_data_start]
//	recovering ──breach sample─────► alerting
//	recovering ──(recovery held ≥ recovery_for)──► ok  [event: resolved]
//	recovering ──(no data timeout)──► no_data  [event: no_data_start]
//	no_data ──good sample──────────► ok       [event: no_data_end]
//	no_data ──breach sample────────► pending  [event: no_data_end]
//
// Time is driven by a single virtual clock advanced monotonically by sample
// timestamps and explicit Tick calls. Duplicate samples (same metric and
// timestamp) are idempotent and never advance durations. Late (out-of-order)
// samples are stored for queries but do not re-evaluate state.
package engine

import (
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"

	"alertfsm/internal/model"
	"alertfsm/internal/store"
)

// DefaultBaseClockMS is the virtual clock used on a fresh database
// (2023-11-14T22:13:20Z, an arbitrary fixed point so runs are reproducible).
const DefaultBaseClockMS = int64(1700000000000)

// Engine applies the state machine over the store.
type Engine struct {
	st *store.Store
}

func New(st *store.Store) *Engine {
	return &Engine{st: st}
}

// Store returns the underlying store (used by the API layer and tests).
func (e *Engine) Store() *store.Store { return e.st }

// Clock returns the current virtual clock.
func (e *Engine) Clock() int64 {
	return e.st.Clock()
}

func (e *Engine) clockLocked() int64 { return e.st.ClockLocked() }

// validateRule checks configuration. nowMS is used for timestamps.
func validateRule(r model.Rule) error {
	var problems []string
	if strings.TrimSpace(r.ID) == "" {
		problems = append(problems, "id is required")
	}
	if strings.TrimSpace(r.Metric) == "" {
		problems = append(problems, "metric is required")
	}
	if r.Direction != model.DirectionAbove && r.Direction != model.DirectionBelow {
		problems = append(problems, `direction must be "above" or "below"`)
	}
	if r.PendingFor < 0 {
		problems = append(problems, "pending_for must be >= 0")
	}
	if r.RecoveryFor < 0 {
		problems = append(problems, "recovery_for must be >= 0")
	}
	if r.NoDataFor < 0 {
		problems = append(problems, "no_data_for must be >= 0")
	}
	if len(problems) > 0 {
		return errors.New(strings.Join(problems, "; "))
	}
	return nil
}

// newStateFor returns the initial state of a freshly created/reset rule.
func newStateFor(r model.Rule, nowMS int64) model.State {
	return model.State{
		RuleID:      r.ID,
		Metric:      r.Metric,
		Status:      model.StatusOK,
		EnteredAtMS: nowMS,
		LastSeenMS:  0, // never
		WatermarkMS: nowMS,
	}
}

// CreateRule adds a new rule.
func (e *Engine) CreateRule(r model.Rule) (model.Rule, error) {
	if err := validateRule(r); err != nil {
		return model.Rule{}, err
	}
	e.st.Lock()
	defer e.st.Unlock()
	if _, exists := e.st.GetRuleLocked(r.ID); exists {
		return model.Rule{}, fmt.Errorf("rule %q already exists", r.ID)
	}
	now := e.clockLocked()
	r.CreatedAtMS = now
	r.UpdatedAtMS = now
	if !r.Enabled {
		r.Enabled = true // rules default to enabled
	}
	e.st.PutRuleLocked(r)
	e.st.PutStateLocked(newStateFor(r, now))
	if err := e.st.SaveLocked(); err != nil {
		return model.Rule{}, err
	}
	return r, nil
}

// UpdateRule replaces the configuration of an existing rule. A configuration
// change explicitly RESETS the rule's state to ok and emits a "reset" event,
// by design: the new thresholds/durations make the old run meaningless.
func (e *Engine) UpdateRule(r model.Rule) (model.Rule, error) {
	if err := validateRule(r); err != nil {
		return model.Rule{}, err
	}
	e.st.Lock()
	defer e.st.Unlock()
	prev, exists := e.st.GetRuleLocked(r.ID)
	if !exists {
		return model.Rule{}, fmt.Errorf("rule %q not found", r.ID)
	}
	now := e.clockLocked()
	r.CreatedAtMS = prev.CreatedAtMS
	r.UpdatedAtMS = now
	r.Enabled = true
	oldState, _ := e.st.GetStateLocked(r.ID)

	e.st.PutRuleLocked(r)
	e.st.PutStateLocked(newStateFor(r, now))
	e.st.AddEventLocked(model.Event{
		TSMS:    now,
		RuleID:  r.ID,
		Metric:  r.Metric,
		Type:    model.EventReset,
		From:    oldState.Status,
		To:      model.StatusOK,
		Message: fmt.Sprintf("rule configuration updated; state reset from %s", oldState.Status),
	})
	if err := e.st.SaveLocked(); err != nil {
		return model.Rule{}, err
	}
	return r, nil
}

// DeleteRule removes a rule and its state. Historical events are retained.
func (e *Engine) DeleteRule(id string) error {
	e.st.Lock()
	defer e.st.Unlock()
	if !e.st.DeleteRuleLocked(id) {
		return fmt.Errorf("rule %q not found", id)
	}
	e.st.DeleteStateLocked(id)
	return e.st.SaveLocked()
}

// IngestResult reports what happened to each submitted sample.
type IngestResult struct {
	ClockMS   int64          `json:"clock_ms"`
	Accepted  []SampleReport `json:"accepted"`
	Duplicate []SampleReport `json:"duplicate"`
	Late      []SampleReport `json:"late"`
}

// SampleReport describes one sample outcome.
type SampleReport struct {
	Metric string  `json:"metric"`
	TSMS   int64   `json:"ts_ms"`
	Value  float64 `json:"value"`
	Reason string  `json:"reason,omitempty"`
}

// Ingest stores and evaluates a batch of samples. Samples with ts_ms == 0 are
// stamped with the current virtual clock. Processing order is by timestamp so
// batching is deterministic regardless of the order the client sends them.
func (e *Engine) Ingest(samples []model.Sample) (IngestResult, error) {
	res := IngestResult{
		Accepted:  []SampleReport{},
		Duplicate: []SampleReport{},
		Late:      []SampleReport{},
	}
	if len(samples) == 0 {
		res.ClockMS = e.Clock()
		return res, nil
	}
	// Validate + stamp.
	ordered := make([]model.Sample, len(samples))
	copy(ordered, samples)
	for i := range ordered {
		if strings.TrimSpace(ordered[i].Metric) == "" {
			return res, errors.New("sample metric is required")
		}
	}

	e.st.Lock()
	defer e.st.Unlock()
	now := e.clockLocked()
	for i := range ordered {
		if ordered[i].TSMS == 0 {
			ordered[i].TSMS = now
		}
	}
	sort.SliceStable(ordered, func(i, j int) bool {
		if ordered[i].TSMS != ordered[j].TSMS {
			return ordered[i].TSMS < ordered[j].TSMS
		}
		return ordered[i].Metric < ordered[j].Metric
	})

	for _, sm := range ordered {
		// Rules share one global clock; the rule watermarks all equal the
		// global watermark, so advance first, then classify the sample.
		clockBefore := e.clockLocked()
		e.advanceToLocked(sm.TSMS)
		report := SampleReport{Metric: sm.Metric, TSMS: sm.TSMS, Value: sm.Value}

		switch {
		case e.st.HasSampleLocked(sm.Metric, sm.TSMS):
			// Duplicate (same metric + timestamp already seen): idempotent,
			// must NOT advance any duration.
			report.Reason = "metric+ts already ingested; durations unchanged"
			res.Duplicate = append(res.Duplicate, report)
			continue
		case sm.TSMS < clockBefore:
			// Out-of-order sample older than already-evaluated time. It is
			// stored for queries but never drives the state machine.
			e.st.AddSampleLocked(sm)
			report.Reason = "older than evaluated watermark; stored but ignored by state machine"
			res.Late = append(res.Late, report)
			continue
		}

		// New, on-time sample: store once and apply to every matching rule.
		e.st.AddSampleLocked(sm)
		rules := e.rulesForMetricLocked(sm.Metric)
		if len(rules) == 0 {
			report.Reason = "stored (no matching rule)"
		}
		for i := range rules {
			r := rules[i]
			if st, ok := e.st.GetStateLocked(r.ID); ok {
				e.applySampleLocked(r, &st, sm)
				e.st.PutStateLocked(st)
			}
		}
		res.Accepted = append(res.Accepted, report)
	}
	res.ClockMS = e.clockLocked()
	if err := e.st.SaveLocked(); err != nil {
		return res, err
	}
	return res, nil
}

// Tick advances the virtual clock to t (or by the current time if t == 0 with
// useWallClock semantics supplied by the caller layer). A pure forward tick
// re-evaluates timer-based transitions (pending -> alerting, recovery -> ok,
// anything -> no_data) for every rule.
func (e *Engine) Tick(t int64) (int64, error) {
	e.st.Lock()
	defer e.st.Unlock()
	if t < e.clockLocked() {
		return e.clockLocked(), fmt.Errorf("clock cannot go backwards: %d < %d", t, e.clockLocked())
	}
	e.advanceToLocked(t)
	if err := e.st.SaveLocked(); err != nil {
		return 0, err
	}
	return e.clockLocked(), nil
}

// rulesForMetricLocked returns enabled rules watching metric (ordered by ID).
func (e *Engine) rulesForMetricLocked(metric string) []model.Rule {
	var out []model.Rule
	for _, r := range e.st.ListRulesLocked() {
		if r.Enabled && r.Metric == metric {
			out = append(out, r)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

// breach reports whether the value is on the breaching side of the threshold.
func breach(r model.Rule, v float64) bool {
	if r.Direction == model.DirectionAbove {
		return v > r.Threshold
	}
	return v < r.Threshold
}

// applySampleLocked handles one new, non-duplicate, non-late sample.
func (e *Engine) applySampleLocked(r model.Rule, st *model.State, sm model.Sample) {
	st.LastSeenMS = sm.TSMS
	st.LastValue = sm.Value
	bad := breach(r, sm.Value)

	switch st.Status {
	case model.StatusOK:
		if bad {
			st.Status = model.StatusPending
			st.EnteredAtMS = sm.TSMS
			// pending_for == 0 fires on the breaching sample itself.
			e.firePendingIfDueLocked(r, st, sm.TSMS)
		}
	case model.StatusPending:
		if bad {
			e.firePendingIfDueLocked(r, st, sm.TSMS)
		} else {
			st.Status = model.StatusOK
			st.EnteredAtMS = sm.TSMS
		}
	case model.StatusAlerting:
		if !bad {
			st.Status = model.StatusRecovering
			st.EnteredAtMS = sm.TSMS
			// recovery_for == 0 resolves on the good sample itself.
			e.resolveRecoveryIfDueLocked(r, st, sm.TSMS)
		}
		// another breaching sample: keep alerting, no event.
	case model.StatusRecovering:
		if bad {
			// Flapped back below threshold before recovery completed:
			// re-enter alerting immediately (we already fired, no new event).
			st.Status = model.StatusAlerting
			st.EnteredAtMS = sm.TSMS
		} else {
			e.resolveRecoveryIfDueLocked(r, st, sm.TSMS)
		}
	case model.StatusNoData:
		// Data resumed. Go to the state the new value implies.
		to := model.StatusOK
		if bad {
			to = model.StatusPending
		}
		e.transitionLocked(r, st, to, sm.TSMS,
			fmt.Sprintf("data resumed with value %g", sm.Value))
		if to == model.StatusPending {
			e.firePendingIfDueLocked(r, st, sm.TSMS)
		}
	}
}

// firePendingIfDueLocked promotes pending -> alerting when the breach run has
// been sustained at least pending_for at time at.
func (e *Engine) firePendingIfDueLocked(r model.Rule, st *model.State, at int64) {
	if st.Status == model.StatusPending && at-st.EnteredAtMS >= r.PendingFor.Milliseconds() {
		e.transitionLocked(r, st, model.StatusAlerting, at,
			fmt.Sprintf("value breach %s threshold %g sustained for %d ms", r.Direction, r.Threshold, at-st.EnteredAtMS))
	}
}

// resolveRecoveryIfDueLocked promotes recovering -> ok when the good run has
// lasted at least recovery_for at time at.
func (e *Engine) resolveRecoveryIfDueLocked(r model.Rule, st *model.State, at int64) {
	if st.Status == model.StatusRecovering && at-st.EnteredAtMS >= r.RecoveryFor.Milliseconds() {
		e.transitionLocked(r, st, model.StatusOK, at,
			fmt.Sprintf("value recovered and held for %d ms", at-st.EnteredAtMS))
	}
}

// transitionLocked emits the notification event for a status change.
func (e *Engine) transitionLocked(r model.Rule, st *model.State, to string, ts int64, msg string) {
	from := st.Status
	et := eventTypeFor(from, to)
	st.Status = to
	st.EnteredAtMS = ts
	if et != "" {
		e.st.AddEventLocked(model.Event{
			TSMS:    ts,
			RuleID:  r.ID,
			Metric:  r.Metric,
			Type:    et,
			From:    from,
			To:      to,
			Message: msg,
		})
	}
}

// eventTypeFor maps a transition to its notification type.
func eventTypeFor(from, to string) string {
	switch {
	case to == model.StatusAlerting:
		return model.EventFiring
	case from == model.StatusRecovering && to == model.StatusOK:
		return model.EventResolved
	case from == model.StatusNoData:
		return model.EventNoDataEnd
	case to == model.StatusNoData:
		return model.EventNoDataStart
	}
	return ""
}

// advanceToLocked advances evaluation up to time t, processing timer-based
// transitions for every rule in timestamp order until all watermarks reach t.
func (e *Engine) advanceToLocked(t int64) {
	if t <= e.clockLocked() {
		return
	}
	for {
		// candidate identifies one possible timer transition.
		type candidate struct {
			ruleID   string
			deadline int64
			kind     string // "nodata" | "pending" | "recovery"
		}
		var best candidate
		found := false
		// better reports whether c should replace best.
		better := func(c candidate) bool {
			if !found {
				return true
			}
			if c.deadline != best.deadline {
				return c.deadline < best.deadline
			}
			// Equal deadline: no-data first, then deterministic rule order.
			cND, bND := c.kind == "nodata", best.kind == "nodata"
			if cND != bND {
				return cND
			}
			return c.ruleID < best.ruleID
		}

		for _, r := range e.st.ListRulesLocked() {
			if !r.Enabled {
				continue
			}
			st, ok := e.st.GetStateLocked(r.ID)
			if !ok || st.WatermarkMS >= t {
				continue
			}

			// Deadline imposed by the current status's holding period.
			if st.Status == model.StatusPending || st.Status == model.StatusRecovering {
				var hold int64
				kind := "pending"
				if st.Status == model.StatusPending {
					hold = r.PendingFor.Milliseconds()
				} else {
					hold = r.RecoveryFor.Milliseconds()
					kind = "recovery"
				}
				d := st.EnteredAtMS + hold
				if d > st.WatermarkMS && d <= t {
					c := candidate{ruleID: r.ID, deadline: d, kind: kind}
					if better(c) {
						best, found = c, true
					}
				}
			}

			// No-data deadline (strictly after the last seen sample; a sample
			// arriving exactly at last_seen + no_data_for is still on time).
			if r.NoDataFor > 0 && st.Status != model.StatusNoData && st.LastSeenMS > 0 {
				nd := st.LastSeenMS + r.NoDataFor.Milliseconds()
				if nd > st.WatermarkMS && nd <= t {
					c := candidate{ruleID: r.ID, deadline: nd, kind: "nodata"}
					if better(c) {
						best, found = c, true
					}
				}
			}
		}
		if !found {
			break
		}

		d := best.deadline
		r, _ := e.st.GetRuleLocked(best.ruleID)
		st, _ := e.st.GetStateLocked(best.ruleID)

		// Time passes globally: advance every rule's watermark to d.
		for _, rr := range e.st.ListRulesLocked() {
			if sst, ok := e.st.GetStateLocked(rr.ID); ok && sst.WatermarkMS < d {
				sst.WatermarkMS = d
				e.st.PutStateLocked(sst)
			}
		}
		e.st.SetClockLocked(d)

		switch best.kind {
		case "pending":
			if st.Status == model.StatusPending {
				e.transitionLocked(r, &st, model.StatusAlerting, d,
					fmt.Sprintf("threshold breach sustained for %d ms", r.PendingFor.Milliseconds()))
			}
		case "recovery":
			if st.Status == model.StatusRecovering {
				e.transitionLocked(r, &st, model.StatusOK, d,
					fmt.Sprintf("recovered and held for %d ms", r.RecoveryFor.Milliseconds()))
			}
		case "nodata":
			if st.Status != model.StatusNoData {
				e.transitionLocked(r, &st, model.StatusNoData, d,
					fmt.Sprintf("no sample for %d ms", r.NoDataFor.Milliseconds()))
			}
		}
		e.st.PutStateLocked(st)
	}

	// Advance remaining watermarks and the global clock straight to t.
	for _, r := range e.st.ListRulesLocked() {
		if st, ok := e.st.GetStateLocked(r.ID); ok && st.WatermarkMS < t {
			st.WatermarkMS = t
			e.st.PutStateLocked(st)
		}
	}
	e.st.SetClockLocked(t)
}

// WallNowMS returns real wall-clock milliseconds (for HTTP convenience).
func WallNowMS() int64 { return time.Now().UnixMilli() }
