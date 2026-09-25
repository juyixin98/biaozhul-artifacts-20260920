package sched

import (
	"encoding/json"
	"fmt"
	"sort"
	"sync"
	"time"
)

// Resource is a multi-dimensional capacity pool.
type Resource struct {
	ID       string    `json:"id"`
	Capacity Dims      `json:"capacity"`
	Created  time.Time `json:"created_at"`
}

func (r *Resource) validate() error {
	if r.ID == "" {
		return validationError(ReasonDuplicateResID, "resource id must not be empty")
	}
	return validateDims(r.Capacity, true, ReasonNegativeCapacity, fmt.Sprintf("resource %q capacity", r.ID))
}

// Reservation statuses.
const (
	// StatusScheduled: committed, start time still in the future.
	StatusScheduled = "scheduled"
	// StatusActive: the clock has reached start and the executor accepted it.
	StatusActive = "active"
	// StatusCompleted: the clock has reached end (active reservations only).
	StatusCompleted = "completed"
	// StatusFailed: the executor rejected activation. The reservation still
	// occupies capacity (it was committed) but will never complete.
	StatusFailed = "activation_failed"
)

// Reservation is one committed piece of capacity usage.
type Reservation struct {
	ID         string    `json:"id"`
	BatchID    string    `json:"batch_id,omitempty"`
	ResourceID string    `json:"resource_id"`
	Interval   Interval  `json:"interval"`
	Demand     Dims      `json:"demand"`
	Status     string    `json:"status"`
	CreatedAt  time.Time `json:"created_at"`
}

// ItemKind selects how an item inside a batch is placed.
type ItemKind string

// Supported item kinds.
const (
	// ItemFixed reserves a specific interval; any conflict rejects the batch.
	ItemFixed ItemKind = "fixed"
	// ItemEarliest asks the solver to pick the earliest feasible slot inside
	// the item's search window. The committed interval is reported back.
	ItemEarliest ItemKind = "earliest"
)

// BatchItem is one reservation request inside an atomic batch.
type BatchItem struct {
	ID         string   `json:"id"`
	ResourceID string   `json:"resource_id"`
	Kind       ItemKind `json:"kind"`
	Demand     Dims     `json:"demand"`
	// Duration is required for both kinds (fixed items may instead set both
	// Interval endpoints; Duration is then derived).
	Duration time.Duration `json:"-"`
	// Interval is used by fixed items.
	Interval Interval `json:"interval,omitempty"`
	// WindowStart/WindowEnd bound the search for earliest items. WindowEnd's
	// zero value means "open-ended" (bounded only by existing reservations'
	// starts; a no-feasible answer is then only possible for capacity
	// reasons).
	WindowStart time.Time `json:"-"`
	WindowEnd   time.Time `json:"-"`
}

// MarshalJSON renders BatchItem, omitting zero-valued window times (plain
// `omitempty` does not apply to the time.Time struct).
func (it BatchItem) MarshalJSON() ([]byte, error) {
	type alias struct {
		ID          string     `json:"id"`
		ResourceID  string     `json:"resource_id"`
		Kind        ItemKind   `json:"kind"`
		Demand      Dims       `json:"demand"`
		Interval    *Interval  `json:"interval,omitempty"`
		Duration    string     `json:"duration,omitempty"`
		WindowStart *time.Time `json:"window_start,omitempty"`
		WindowEnd   *time.Time `json:"window_end,omitempty"`
	}
	a := alias{ID: it.ID, ResourceID: it.ResourceID, Kind: it.Kind, Demand: it.Demand}
	if it.Kind == ItemFixed {
		in := it.Interval
		a.Interval = &in
	} else {
		if it.Duration != 0 {
			a.Duration = it.Duration.String()
		}
		if !it.WindowStart.IsZero() {
			t := it.WindowStart
			a.WindowStart = &t
		}
		if !it.WindowEnd.IsZero() {
			t := it.WindowEnd
			a.WindowEnd = &t
		}
	}
	return json.Marshal(a)
}

// PlannedItem is the solver's result for one batch item.
type PlannedItem struct {
	Item  BatchItem `json:"item"`
	Start time.Time `json:"start"`
	End   time.Time `json:"end"`
}

// BatchRequest is an atomic set of reservation items.
type BatchRequest struct {
	ID        string      `json:"id"`
	Items     []BatchItem `json:"items"`
	RequestID string      `json:"request_id,omitempty"`
}

// Batch is a committed batch request plus its planned placements.
type Batch struct {
	ID             string        `json:"id"`
	RequestID      string        `json:"request_id,omitempty"`
	Items          []BatchItem   `json:"items"`
	Planned        []PlannedItem `json:"planned"`
	ReservationIDs []string      `json:"reservation_ids"`
	CommittedAt    time.Time     `json:"committed_at"`
}

// ItemConflict explains why one batch item could not be placed.
type ItemConflict struct {
	ItemID    string     `json:"item_id"`
	Kind      ItemKind   `json:"kind"`
	Reason    string     `json:"reason"`
	Conflicts []Conflict `json:"conflicts,omitempty"`
}

// BatchConflictError rejects a batch; nothing was committed.
type BatchConflictError struct {
	BatchID string         `json:"batch_id"`
	Items   []ItemConflict `json:"items"`
}

func (e *BatchConflictError) Error() string {
	return fmt.Sprintf("batch %q rejected: %d conflicting item(s)", e.BatchID, len(e.Items))
}

// Executor reacts to reservation lifecycle transitions. Implementations must
// be safe for concurrent use. Returning an error from Activate moves the
// reservation to status "activation_failed" (capacity stays occupied);
// Complete errors are recorded but the reservation is marked completed.
type Executor interface {
	Activate(r *Reservation) error
	Complete(r *Reservation) error
}

// NopExecutor accepts every transition and returns no error.
type NopExecutor struct{}

// Activate does nothing.
func (NopExecutor) Activate(*Reservation) error { return nil }

// Complete does nothing.
func (NopExecutor) Complete(*Reservation) error { return nil }

// Scheduler is the in-memory reservation store and solver.
type Scheduler struct {
	clock    Clock
	exec     Executor
	sink     Sink
	idPrefix string

	mu         sync.Mutex
	resources  map[string]*Resource
	resByID    map[string]*Reservation
	resByRes   map[string][]*Reservation
	batches    map[string]*Batch
	seq        int64
	nextResNum int64
	pumpMu     sync.Mutex
}

// Option configures a Scheduler.
type Option func(*Scheduler)

// WithClock replaces the scheduling clock.
func WithClock(c Clock) Option { return func(s *Scheduler) { s.clock = c } }

// WithExecutor replaces the lifecycle executor.
func WithExecutor(e Executor) Option { return func(s *Scheduler) { s.exec = e } }

// WithSink adds a structured-event sink.
func WithSink(sink Sink) Option { return func(s *Scheduler) { s.sink = sink } }

// WithIDPrefix sets the prefix for generated reservation ids.
func WithIDPrefix(p string) Option { return func(s *Scheduler) { s.idPrefix = p } }

// New creates a Scheduler.
func New(opts ...Option) *Scheduler {
	s := &Scheduler{
		clock:     SystemClock{},
		exec:      NopExecutor{},
		sink:      NewEventLog(),
		idPrefix:  "r",
		resources: map[string]*Resource{},
		resByID:   map[string]*Reservation{},
		resByRes:  map[string][]*Reservation{},
		batches:   map[string]*Batch{},
	}
	for _, o := range opts {
		o(s)
	}
	return s
}

// EventLog returns the in-memory log when the scheduler's sink chain
// contains one (the default), or nil otherwise.
func (s *Scheduler) EventLog() *EventLog {
	var walk func(sink Sink) *EventLog
	walk = func(sink Sink) *EventLog {
		switch v := sink.(type) {
		case *EventLog:
			return v
		case *multiSink:
			for _, sub := range v.sinks {
				if l := walk(sub); l != nil {
					return l
				}
			}
		}
		return nil
	}
	return walk(s.sink)
}

func (s *Scheduler) emitLocked(ev Event) Event {
	s.seq++
	ev.Seq = s.seq
	if ev.Timestamp.IsZero() {
		ev.Timestamp = s.clock.Now()
	}
	s.sink.Write(ev)
	return ev
}

// AddResource registers a capacity pool.
func (s *Scheduler) AddResource(r *Resource) error {
	if err := r.validate(); err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, exists := s.resources[r.ID]; exists {
		return validationError(ReasonDuplicateResID, "resource %q already exists", r.ID)
	}
	rc := *r
	rc.Capacity = cloneDims(r.Capacity)
	if rc.Created.IsZero() {
		rc.Created = s.clock.Now()
	}
	s.resources[r.ID] = &rc
	s.emitLocked(Event{Type: EventResourceAdded, Resource: r.ID, Payload: &rc})
	return nil
}

// GetResource returns a registered resource.
func (s *Scheduler) GetResource(id string) (*Resource, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.resources[id]
	if !ok {
		return nil, false
	}
	rc := *r
	rc.Capacity = cloneDims(r.Capacity)
	return &rc, true
}

// Resources returns all registered resources, sorted by id.
func (s *Scheduler) Resources() []Resource {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]Resource, 0, len(s.resources))
	for _, r := range s.resources {
		out = append(out, *r)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

func (s *Scheduler) existingLocked(resID string, include func(*Reservation) bool) []existing {
	var out []existing
	for _, r := range s.resByRes[resID] {
		if include == nil || include(r) {
			out = append(out, existing{id: r.ID, res: r.ResourceID, interval: r.Interval, demand: cloneDims(r.Demand)})
		}
	}
	return out
}

// EarliestFeasible answers the earliest-slot query for one would-be
// reservation without committing anything.
func (s *Scheduler) EarliestFeasible(resID string, windowStart, windowEnd time.Time, duration time.Duration, demand Dims) (time.Time, bool, error) {
	if duration <= 0 {
		return time.Time{}, false, validationError(ReasonZeroDuration, "duration must be positive, got %s", duration)
	}
	if err := validateDims(demand, true, ReasonNegativeDemand, "demand"); err != nil {
		return time.Time{}, false, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.resources[resID]
	if !ok {
		return time.Time{}, false, validationError(ReasonResourceNotFound, "resource %q not found", resID)
	}
	for k := range demand {
		if _, ok := r.Capacity[k]; !ok {
			return time.Time{}, false, validationError(ReasonUnknownDimension, "resource %q has no dimension %q", resID, k)
		}
	}
	cur := s.existingLocked(resID, nil)
	t, ok := EarliestFeasible(r.Capacity, cur, windowStart.UTC(), windowEnd.UTC(), duration, cloneDims(demand))
	return t, ok, nil
}
