package budget

import (
	"sync"
	"time"
)

// EventType enumerates the structured state transitions a Sink may record.
type EventType string

const (
	// EventAcquire is emitted for every acquisition attempt, allowed or not.
	EventAcquire EventType = "acquire"
	// EventConfig is emitted when the rate/burst configuration changes.
	EventConfig EventType = "config"
	// EventExecute is emitted by the scheduling executor when a job has run.
	EventExecute EventType = "execute"
)

// DecisionResult is the outcome of an acquisition.
type DecisionResult string

const (
	// Allowed means both global and tenant buckets paid the cost atomically.
	Allowed DecisionResult = "allowed"
	// DeniedGlobal means the global bucket could not cover the cost.
	DeniedGlobal DecisionResult = "denied_global"
	// DeniedTenant means the tenant bucket could not cover the cost.
	DeniedTenant DecisionResult = "denied_tenant"
)

// Event is a structured, machine-readable record of one state change.
//
// All token amounts are exact decimal strings in microtoken units; times are
// the scheduler clock's readings. Events are delivered after the state change
// they describe commits (for denied attempts nothing commits, and the event
// says so).
type Event struct {
	Type       EventType       `json:"type"`
	At         time.Time       `json:"at"`
	RequestID  string          `json:"request_id,omitempty"`
	Tenant     string          `json:"tenant,omitempty"`
	Result     DecisionResult  `json:"result,omitempty"`
	Cost       string          `json:"cost,omitempty"`
	Global     *BucketSnapshot `json:"global,omitempty"`
	TenantSnap *BucketSnapshot `json:"tenant_bucket,omitempty"`
	RetryAfter time.Duration   `json:"retry_after,omitempty"`
	Wait       time.Duration   `json:"wait,omitempty"`
	Detail     string          `json:"detail,omitempty"`
	// Config carries the new configuration on EventConfig.
	Config *ConfigView `json:"config,omitempty"`
}

// BucketSnapshot is an immutable view of one bucket captured at event time.
type BucketSnapshot struct {
	Available string    `json:"available"`
	Capacity  string    `json:"capacity"`
	Rate      string    `json:"rate"`
	Last      time.Time `json:"last_refill"`
}

// Sink receives events. Implementations must be safe for concurrent use and
// must not block the limiter's critical section (calls happen outside locks).
type Sink interface {
	Record(Event)
}

// MemorySink keeps the most recent events in memory. It is safe for
// concurrent use and primarily backs the /events endpoint and tests.
type MemorySink struct {
	mu     sync.Mutex
	events []Event
	cap    int
}

// NewMemorySink creates a ring buffer retaining at most capacity events
// (capacity <= 0 means 1024).
func NewMemorySink(capacity int) *MemorySink {
	if capacity <= 0 {
		capacity = 1024
	}
	return &MemorySink{cap: capacity}
}

// Record implements Sink.
func (m *MemorySink) Record(e Event) {
	m.mu.Lock()
	m.events = append(m.events, e)
	if len(m.events) > m.cap {
		m.events = m.events[len(m.events)-m.cap:]
	}
	m.mu.Unlock()
}

// Events returns a copy of retained events in emission order, optionally
// filtered by tenant ("" means all; the global-only tenant id is "_global").
func (m *MemorySink) Events(tenant string, limit int) []Event {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]Event, 0, len(m.events))
	for _, e := range m.events {
		if tenant != "" && e.Tenant != tenant {
			continue
		}
		out = append(out, e)
	}
	if limit > 0 && len(out) > limit {
		out = out[len(out)-limit:]
	}
	return out
}

// MultiSink fans each event out to every contained sink.
type MultiSink []Sink

// Record implements Sink.
func (ms MultiSink) Record(e Event) {
	for _, s := range ms {
		s.Record(e)
	}
}

// DiscardSink drops everything.
type DiscardSink struct{}

// Record implements Sink.
func (DiscardSink) Record(Event) {}
