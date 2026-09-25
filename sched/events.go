package sched

import (
	"encoding/json"
	"os"
	"sync"
	"time"
)

// Clock is the replaceable scheduling clock. Tests pass a fake clock; the
// production scheduler uses SystemClock.
type Clock interface {
	Now() time.Time
}

// SystemClock reads time.Now (UTC).
type SystemClock struct{}

// Now returns the current wall-clock time in UTC.
func (SystemClock) Now() time.Time { return time.Now().UTC() }

// FakeClock is a manually controlled Clock for deterministic tests.
type FakeClock struct {
	mu sync.Mutex
	t  time.Time
}

// NewFakeClock starts the clock at t.
func NewFakeClock(t time.Time) *FakeClock { return &FakeClock{t: t.UTC()} }

// Now returns the fake current time.
func (c *FakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

// Advance moves the clock forward and returns the new time.
func (c *FakeClock) Advance(d time.Duration) time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = c.t.Add(d)
	return c.t
}

// Set assigns the clock's time.
func (c *FakeClock) Set(t time.Time) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = t.UTC()
}

// Event types emitted on every state change.
const (
	EventResourceAdded   = "resource_added"
	EventBatchCommitted  = "batch_committed"
	EventBatchRejected   = "batch_rejected"
	EventReserved        = "reserved"
	EventActivated       = "activated"
	EventActivationError = "activation_error"
	EventCompleted       = "completed"
)

// Event is one structured state-change record. Payload carries
// event-specific data (e.g. *Batch, *Reservation or an executor error).
type Event struct {
	Seq           int64       `json:"seq"`
	Type          string      `json:"type"`
	Timestamp     time.Time   `json:"timestamp"`
	RequestID     string      `json:"request_id,omitempty"`
	BatchID       string      `json:"batch_id,omitempty"`
	Resource      string      `json:"resource_id,omitempty"`
	ReservationID string      `json:"reservation_id,omitempty"`
	Payload       interface{} `json:"payload,omitempty"`
}

// Sink receives structured events. Implementations must be safe for concurrent
// use and should return promptly; the scheduler dispatches events outside its
// state lock. A sink error does not roll back the state change (sinks are
// observability, not participants in the transaction).
type Sink interface {
	Write(ev Event)
}

// EventLog is an in-memory, concurrency-safe Sink that retains every event.
type EventLog struct {
	mu     sync.RWMutex
	events []Event
}

// NewEventLog creates an empty event log.
func NewEventLog() *EventLog { return &EventLog{} }

// Write appends an event.
func (l *EventLog) Write(ev Event) {
	l.mu.Lock()
	l.events = append(l.events, ev)
	l.mu.Unlock()
}

// Events returns a copy of all recorded events.
func (l *EventLog) Events() []Event {
	l.mu.RLock()
	defer l.mu.RUnlock()
	out := make([]Event, len(l.events))
	copy(out, l.events)
	return out
}

// Since returns events with sequence number strictly greater than afterSeq.
func (l *EventLog) Since(afterSeq int64) []Event {
	l.mu.RLock()
	defer l.mu.RUnlock()
	var out []Event
	for _, e := range l.events {
		if e.Seq > afterSeq {
			out = append(out, e)
		}
	}
	return out
}

// Len returns the number of recorded events.
func (l *EventLog) Len() int {
	l.mu.RLock()
	defer l.mu.RUnlock()
	return len(l.events)
}

// JSONLSink writes events as newline-delimited JSON to a file (creating or
// appending). Useful for local auditing.
type JSONLSink struct {
	mu sync.Mutex
	f  *os.File
}

// NewJSONLSink opens path for append (created if missing).
func NewJSONLSink(path string) (*JSONLSink, error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return nil, err
	}
	return &JSONLSink{f: f}, nil
}

// Write appends one JSON-encoded event followed by a newline.
func (s *JSONLSink) Write(ev Event) {
	b, err := json.Marshal(ev)
	if err != nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	_, _ = s.f.Write(append(b, '\n'))
}

// Close releases the underlying file.
func (s *JSONLSink) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.f.Close()
}

// MultiSink fans every event out to the given sinks.
func MultiSink(sinks ...Sink) Sink { return &multiSink{sinks: sinks} }

type multiSink struct{ sinks []Sink }

func (m *multiSink) Write(ev Event) {
	for _, s := range m.sinks {
		s.Write(ev)
	}
}
