package queue

import (
	"bufio"
	"encoding/json"
	"io"
	"os"
	"sync"
	"time"
)

// EventType enumerates the structured state transitions a job can emit.
type EventType string

const (
	EventSubmitted      EventType = "submitted"       // job entered the queue
	EventStarted        EventType = "started"         // an attempt began executing
	EventPromoted       EventType = "promoted"        // effective priority rose by one level
	EventRetrying       EventType = "retrying"        // attempt failed; scheduled for retry
	EventSucceeded      EventType = "succeeded"       // terminal success
	EventFailed         EventType = "failed"          // terminal failure (attempts exhausted / fatal)
	EventCanceled       EventType = "canceled"        // terminal cancellation
	EventCancelRejected EventType = "cancel_rejected" // cancel hit unknown/terminal id
)

// Event is one immutable structured state-change record.
type Event struct {
	Time  time.Time `json:"time"`
	Type  EventType `json:"type"`
	JobID string    `json:"jobId"`
	// Detail carries type-specific fields; the common ones below are the
	// documented keys (unknown keys may appear in future versions).
	Detail map[string]any `json:"detail,omitempty"`
}

// Sink receives the event stream. Implementations must be safe for
// concurrent use; the scheduler calls Emit asynchronously, so a slow sink
// never blocks scheduling.
type Sink interface {
	Emit(Event)
}

// MemorySink keeps events in memory. With Capacity > 0 it is a ring buffer
// that drops the OLDEST event when full; Capacity 0 means unbounded.
type MemorySink struct {
	mu       sync.Mutex
	events   []Event
	capacity int
	cond     *sync.Cond
}

// NewMemorySink creates a MemorySink (capacity 0 = unbounded).
func NewMemorySink(capacity int) *MemorySink {
	s := &MemorySink{capacity: capacity}
	s.cond = sync.NewCond(&s.mu)
	return s
}

func (s *MemorySink) Emit(e Event) {
	s.mu.Lock()
	if s.capacity > 0 && len(s.events) >= s.capacity {
		s.events = append(s.events[1:], e)
	} else {
		s.events = append(s.events, e)
	}
	s.cond.Broadcast()
	s.mu.Unlock()
}

// Events returns a copy of all retained events in emission order.
func (s *MemorySink) Events() []Event {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]Event, len(s.events))
	copy(out, s.events)
	return out
}

// Len returns the number of retained events.
func (s *MemorySink) Len() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.events)
}

// WaitFor blocks until at least n events exist or timeout elapses. It is a
// test helper for the wall-clock executor; the fake clock needs no waiting.
func (s *MemorySink) WaitFor(n int, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	for {
		if s.Len() >= n {
			return true
		}
		if time.Now().After(deadline) {
			return false
		}
		time.Sleep(2 * time.Millisecond)
	}
}

// MultiSink fans every event out to all wrapped sinks.
type MultiSink []Sink

func (m MultiSink) Emit(e Event) {
	for _, s := range m {
		s.Emit(e)
	}
}

// JSONLSink appends events as newline-delimited JSON to a file (or any
// io.Writer). Flushed after every event so external readers see records
// promptly; writes are serialized.
type JSONLSink struct {
	mu sync.Mutex
	w  io.Writer
	f  *os.File
}

// NewJSONLFileSink creates (or appends to) path.
func NewJSONLFileSink(path string) (*JSONLSink, error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return nil, err
	}
	return &JSONLSink{w: bufio.NewWriter(f), f: f}, nil
}

// NewJSONLSink writes events to w.
func NewJSONLSink(w io.Writer) *JSONLSink {
	return &JSONLSink{w: w}
}

func (s *JSONLSink) Emit(e Event) {
	b, err := json.Marshal(e)
	if err != nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	_, _ = s.w.Write(b)
	_, _ = s.w.Write([]byte{'\n'})
	if bw, ok := s.w.(*bufio.Writer); ok {
		_ = bw.Flush()
	}
}

// Close flushes and closes the backing file when one was opened via
// NewJSONLFileSink.
func (s *JSONLSink) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.f == nil {
		return nil
	}
	if bw, ok := s.w.(*bufio.Writer); ok {
		_ = bw.Flush()
	}
	return s.f.Close()
}
