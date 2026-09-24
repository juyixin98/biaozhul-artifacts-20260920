// Package engine is the time-agnostic trigger core. All progression happens
// through Advance(now); production code feeds wall-clock time while tests
// drive a FakeClock deterministically, so no test ever sleeps.
package engine

import (
	"errors"
	"fmt"
	"sort"
	"sync"
	"time"

	"tztrig/internal/schedule"
	"tztrig/internal/zoneinfo"
)

// Reason describes why a firing was produced.
type Reason string

const (
	ReasonDue     Reason = "due"     // scheduled instant has arrived
	ReasonCatchUp Reason = "catchup" // missed while the service was stopped
)

// Fire is one delivered trigger.
type Fire struct {
	ID         string    `json:"id"` // scheduleID + ":" + unix seconds; globally unique per logical trigger
	ScheduleID string    `json:"scheduleId"`
	EventTime  time.Time `json:"eventTime"` // the wall instant the expression defined
	Reason     Reason    `json:"reason"`
	FiredAt    time.Time `json:"firedAt"` // when the engine processed it
}

// AdvanceResult summarizes one progression step.
type AdvanceResult struct {
	Fired   []Fire       `json:"fired"`
	Skipped []SkipRecord `json:"skipped"`
}

// SkipRecord reports firings deliberately dropped by the catch-up cap.
type SkipRecord struct {
	ScheduleID string    `json:"scheduleId"`
	Count      int       `json:"count"`
	Oldest     time.Time `json:"oldest"` // oldest dropped instant
	Newest     time.Time `json:"newest"` // newest dropped instant (just before the kept window)
}

// ErrNotFound is returned for unknown schedule IDs.
var ErrNotFound = errors.New("schedule not found")

// ScheduleState is the stored representation of one trigger definition.
type ScheduleState struct {
	ID        string    `json:"id"`
	Minute    string    `json:"minute"`
	Hour      string    `json:"hour"`
	Weekday   string    `json:"weekday"`
	Timezone  string    `json:"timezone"`
	Enabled   bool      `json:"enabled"`
	CreatedAt time.Time `json:"createdAt"`
	// LastFired is the high-water mark: only instants strictly after it can
	// fire. Skipped catch-up instants still advance it.
	LastFired *time.Time `json:"lastFired"`
}

// Clock is the engine's only source of time.
type Clock interface {
	Now() time.Time
}

// RealClock follows the host wall clock.
type RealClock struct{}

// Now returns the current UTC time.
func (RealClock) Now() time.Time { return time.Now().UTC() }

// FakeClock is moved manually in tests.
type FakeClock struct {
	mu sync.Mutex
	t  time.Time
}

// NewFakeClock starts at t (interpreted as UTC if unset).
func NewFakeClock(t time.Time) *FakeClock { return &FakeClock{t: t.UTC()} }

// Now reports the simulated time.
func (c *FakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

// Set jumps the clock to t.
func (c *FakeClock) Set(t time.Time) {
	c.mu.Lock()
	c.t = t.UTC()
	c.mu.Unlock()
}

// Advance moves the clock forward.
func (c *FakeClock) Advance(d time.Duration) {
	c.mu.Lock()
	c.t = c.t.Add(d)
	c.mu.Unlock()
}

// Engine owns all schedules and their execution state.
type Engine struct {
	mu        sync.Mutex
	clock     Clock
	schedules map[string]*ScheduleState
	fired     map[string]time.Time // id -> processed-at, for idempotent dedupe
	history   []Fire
	lastSkips []SkipRecord // overflows from the most recent Advance
	catchUp   int
	store     Store
	onFire    func(Fire)
}

// Store persists snapshots after every mutation (may be nil).
type Store interface {
	Save(*Snapshot) error
}

// Option configures New.
type Option func(*Engine)

// WithCatchUpLimit sets the maximum number of missed firings delivered per
// schedule when the service catches up after downtime.
func WithCatchUpLimit(n int) Option {
	return func(e *Engine) { e.catchUp = n }
}

// WithStore attaches persistence.
func WithStore(s Store) Option { return func(e *Engine) { e.store = s } }

// WithFireCallback installs a hook invoked synchronously for every fire.
func WithFireCallback(fn func(Fire)) Option { return func(e *Engine) { e.onFire = fn } }

// New constructs an engine. The default catch-up limit is 100.
func New(clock Clock, opts ...Option) *Engine {
	e := &Engine{
		clock:     clock,
		schedules: map[string]*ScheduleState{},
		fired:     map[string]time.Time{},
		catchUp:   100,
	}
	for _, o := range opts {
		o(e)
	}
	if e.catchUp < 1 {
		e.catchUp = 1
	}
	return e
}

// CreateInput is the validated create/update payload.
type CreateInput struct {
	ID       string `json:"id"`
	Minute   string `json:"minute"`
	Hour     string `json:"hour"`
	Weekday  string `json:"weekday"`
	Timezone string `json:"timezone"`
	Enabled  *bool  `json:"enabled"`
}

// CreateSchedule validates and stores a new schedule.
func (e *Engine) CreateSchedule(in CreateInput) (*ScheduleState, error) {
	if in.ID == "" {
		return nil, errors.New("id is required")
	}
	loc, err := zoneinfo.Load(in.Timezone)
	if err != nil {
		return nil, err
	}
	if _, err := schedule.Parse(in.Minute, in.Hour, in.Weekday, loc); err != nil {
		return nil, err
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if _, exists := e.schedules[in.ID]; exists {
		return nil, fmt.Errorf("schedule %q already exists", in.ID)
	}
	enabled := true
	if in.Enabled != nil {
		enabled = *in.Enabled
	}
	st := &ScheduleState{
		ID: in.ID, Minute: in.Minute, Hour: in.Hour, Weekday: in.Weekday,
		Timezone: in.Timezone, Enabled: enabled, CreatedAt: e.clock.Now().UTC(),
	}
	e.schedules[in.ID] = st
	e.persistLocked()
	return cloneState(st), nil
}

// DeleteSchedule removes a schedule.
func (e *Engine) DeleteSchedule(id string) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	if _, ok := e.schedules[id]; !ok {
		return ErrNotFound
	}
	delete(e.schedules, id)
	e.persistLocked()
	return nil
}

// SetEnabled pauses or resumes a schedule.
func (e *Engine) SetEnabled(id string, enabled bool) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	st, ok := e.schedules[id]
	if !ok {
		return ErrNotFound
	}
	st.Enabled = enabled
	e.persistLocked()
	return nil
}

// Get returns a clone of one schedule.
func (e *Engine) Get(id string) (*ScheduleState, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	st, ok := e.schedules[id]
	if !ok {
		return nil, ErrNotFound
	}
	return cloneState(st), nil
}

// List returns clones of all schedules sorted by ID.
func (e *Engine) List() []ScheduleState {
	e.mu.Lock()
	defer e.mu.Unlock()
	out := make([]ScheduleState, 0, len(e.schedules))
	for _, st := range e.schedules {
		out = append(out, *cloneState(st))
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

// History returns the most recent delivered firings (newest last).
func (e *Engine) History(limit int) []Fire {
	e.mu.Lock()
	defer e.mu.Unlock()
	if limit <= 0 || limit > len(e.history) {
		limit = len(e.history)
	}
	out := make([]Fire, limit)
	copy(out, e.history[len(e.history)-limit:])
	return out
}

// compiled resolves a stored state to an executable expression.
func (e *Engine) compiled(st *ScheduleState) (*schedule.Schedule, error) {
	loc, err := zoneinfo.Load(st.Timezone)
	if err != nil {
		return nil, err
	}
	return schedule.Parse(st.Minute, st.Hour, st.Weekday, loc)
}

// NextFires returns up to n upcoming fire instants for one schedule as of now.
func (e *Engine) NextFires(id string, n int) ([]time.Time, error) {
	if n <= 0 {
		n = 1
	}
	e.mu.Lock()
	st, ok := e.schedules[id]
	e.mu.Unlock()
	if !ok {
		return nil, ErrNotFound
	}
	sched, err := e.compiled(st)
	if err != nil {
		return nil, err
	}
	anchor := e.clock.Now()
	if st.LastFired != nil && st.LastFired.After(anchor) {
		anchor = *st.LastFired
	}
	var out []time.Time
	t := anchor
	for i := 0; i < n; i++ {
		next, ok := sched.Next(t)
		if !ok {
			break
		}
		out = append(out, next)
		t = next
	}
	return out, nil
}

// Advance processes every firing due at or before now (including catch-up
// after downtime), applying the per-schedule limit and logical-ID dedupe.
func (e *Engine) Advance(now time.Time) AdvanceResult {
	now = now.UTC()
	e.mu.Lock()
	defer e.mu.Unlock()

	var res AdvanceResult
	e.lastSkips = nil
	ids := make([]string, 0, len(e.schedules))
	for id := range e.schedules {
		ids = append(ids, id)
	}
	sort.Strings(ids)

	for _, id := range ids {
		st := e.schedules[id]
		if !st.Enabled {
			continue
		}
		sched, err := e.compiled(st)
		if err != nil {
			continue // a renamed/removed embedded zone: skip, never panic
		}
		anchor := st.CreatedAt
		if st.LastFired != nil {
			anchor = *st.LastFired
		}
		var due []time.Time
		sched.ForEachBetween(anchor, now, func(t time.Time) bool {
			due = append(due, t)
			return true
		})
		if len(due) == 0 {
			continue
		}

		skipped := 0
		keep := due
		if len(due) > e.catchUp {
			skipped = len(due) - e.catchUp
			res.Skipped = append(res.Skipped, SkipRecord{
				ScheduleID: id, Count: skipped,
				Oldest: due[0], Newest: due[skipped-1],
			})
			keep = due[skipped:]
		}

		for _, t := range keep {
			fid := fmt.Sprintf("%s:%d", id, t.Unix())
			if _, dup := e.fired[fid]; dup {
				// Same logical trigger already delivered (e.g. overlapping
				// Advance calls after a clock read race): execute once.
				continue
			}
			reason := ReasonDue
			if now.Sub(t) > time.Minute {
				reason = ReasonCatchUp
			}
			f := Fire{
				ID: fid, ScheduleID: id, EventTime: t,
				Reason: reason, FiredAt: now,
			}
			e.fired[fid] = now
			e.history = append(e.history, f)
			res.Fired = append(res.Fired, f)
			st.LastFired = ptrTime(t)
			if e.onFire != nil {
				e.onFire(f)
			}
		}
		// Advance the watermark past dropped firings too, so a downtime that
		// overflowed the cap never causes unbounded backlog enumeration.
		st.LastFired = ptrTime(due[len(due)-1])
	}

	if len(e.history) > 500 {
		e.history = e.history[len(e.history)-500:]
	}
	// Fired-ID table only needs to cover the dedupe window; watermarks make
	// older entries unreachable. Prune entries older than 30 days.
	cutoff := now.Add(-30 * 24 * time.Hour)
	for fid, at := range e.fired {
		if at.Before(cutoff) {
			delete(e.fired, fid)
		}
	}
	if len(res.Fired) > 0 || len(res.Skipped) > 0 {
		e.persistLocked()
	}
	if len(res.Skipped) > 0 {
		e.lastSkips = append([]SkipRecord(nil), res.Skipped...)
	}
	return res
}

// LastSkips returns catch-up overflows recorded by the latest Advance.
func (e *Engine) LastSkips() []SkipRecord {
	e.mu.Lock()
	defer e.mu.Unlock()
	out := make([]SkipRecord, len(e.lastSkips))
	copy(out, e.lastSkips)
	return out
}

// NextWakeup reports the earliest upcoming fire across enabled schedules.
func (e *Engine) NextWakeup() (time.Time, string, bool) {
	e.mu.Lock()
	defer e.mu.Unlock()
	now := e.clock.Now()
	var best time.Time
	var bestID string
	for _, st := range e.schedules {
		if !st.Enabled {
			continue
		}
		sched, err := e.compiled(st)
		if err != nil {
			continue
		}
		next, ok := sched.Next(now)
		if !ok {
			continue
		}
		if best.IsZero() || next.Before(best) {
			best, bestID = next, st.ID
		}
	}
	return best, bestID, !best.IsZero()
}

// CatchUpLimit exposes the configured limit.
func (e *Engine) CatchUpLimit() int {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.catchUp
}

func cloneState(st *ScheduleState) *ScheduleState {
	c := *st
	if st.LastFired != nil {
		c.LastFired = ptrTime(*st.LastFired)
	}
	return &c
}

func ptrTime(t time.Time) *time.Time { u := t.UTC(); return &u }
