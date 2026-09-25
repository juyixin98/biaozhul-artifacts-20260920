package batchagg

import (
	"encoding/json"
	"io"
	"log/slog"
	"sync"
	"time"
)

// EventType enumerates the scheduler's structured state transitions. Every
// transition of an item or batch emits exactly one event.
type EventType string

const (
	// EventSubmitted: an item was accepted into a compatibility-key queue.
	EventSubmitted EventType = "submitted"
	// EventRejected: an item was refused before queueing (oversize, bad key,
	// scheduler shutting down).
	EventRejected EventType = "rejected"
	// EventBatchOpen: a new batch was created for a key.
	EventBatchOpen EventType = "batch_open"
	// EventBatchFlush: a batch was handed to the executor; Reason is one of
	// "items", "bytes", "wait", "shutdown", "cancel".
	EventBatchFlush EventType = "batch_flush"
	// EventItemCanceled: a queued item was removed because its context ended.
	EventItemCanceled EventType = "item_canceled"
	// EventItemResult: an item received its independent result (success or
	// failure) after a batch executed.
	EventItemResult EventType = "item_result"
)

// Event is one structured state-change record. Numeric fields that do not
// apply to a type are left at their zero value; callers should rely on Type.
type Event struct {
	Type     EventType      `json:"type"`
	At       time.Time      `json:"at"`
	Key      string         `json:"key,omitempty"`
	ItemID   string         `json:"item_id,omitempty"`
	BatchID  string         `json:"batch_id,omitempty"`
	Reason   string         `json:"reason,omitempty"`
	Items    int            `json:"items,omitempty"`
	Bytes    int            `json:"bytes,omitempty"`
	MaxItems int            `json:"max_items,omitempty"`
	MaxBytes int            `json:"max_bytes,omitempty"`
	Success  bool           `json:"success,omitempty"`
	Err      string         `json:"err,omitempty"`
	Extra    map[string]any `json:"extra,omitempty"`
}

// EventSink receives state-change events. Implementations must be safe for
// concurrent use; Emit must not block on scheduler progress.
type EventSink interface {
	Emit(Event)
}

// EventSinkFunc adapts a plain function to an EventSink.
type EventSinkFunc func(Event)

// Emit implements EventSink.
func (f EventSinkFunc) Emit(e Event) { f(e) }

// NopSink drops all events.
func NopSink() EventSink { return EventSinkFunc(func(Event) {}) }

// MultiSink fans every event out to the given sinks.
func MultiSink(sinks ...EventSink) EventSink {
	return EventSinkFunc(func(e Event) {
		for _, s := range sinks {
			s.Emit(e)
		}
	})
}

// EventRecorder stores events in memory, in arrival order. It is safe for
// concurrent use and intended mainly for tests.
type EventRecorder struct {
	mu     sync.Mutex
	events []Event
}

// NewEventRecorder creates an empty recorder.
func NewEventRecorder() *EventRecorder { return &EventRecorder{} }

// Emit appends one event.
func (r *EventRecorder) Emit(e Event) {
	r.mu.Lock()
	r.events = append(r.events, e)
	r.mu.Unlock()
}

// Events returns a copy of the recorded events.
func (r *EventRecorder) Events() []Event {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]Event, len(r.events))
	copy(out, r.events)
	return out
}

// OfType returns recorded events of the given type.
func (r *EventRecorder) OfType(t EventType) []Event {
	var out []Event
	for _, e := range r.Events() {
		if e.Type == t {
			out = append(out, e)
		}
	}
	return out
}

// JSONLinesSink writes each event as one JSON object followed by '\n'.
// Writes are serialized; a write error is reported to the writer once and
// further events are dropped.
type JSONLinesSink struct {
	mu  sync.Mutex
	w   io.Writer
	enc *json.Encoder
}

// NewJSONLinesSink creates a newline-delimited JSON sink writing to w.
func NewJSONLinesSink(w io.Writer) *JSONLinesSink {
	s := &JSONLinesSink{w: w}
	s.enc = json.NewEncoder(w)
	return s
}

// Emit writes the event as a JSON line.
func (s *JSONLinesSink) Emit(e Event) {
	s.mu.Lock()
	defer s.mu.Unlock()
	_ = s.enc.Encode(e)
}

// SlogSink logs events through log/slog at Info level.
type SlogSink struct{ L *slog.Logger }

// NewSlogSink wraps l (defaults to slog.Default() when nil).
func NewSlogSink(l *slog.Logger) *SlogSink {
	if l == nil {
		l = slog.Default()
	}
	return &SlogSink{L: l}
}

// Emit logs the event.
func (s *SlogSink) Emit(e Event) {
	args := []any{"type", e.Type, "at", e.At}
	if e.Key != "" {
		args = append(args, "key", e.Key)
	}
	if e.ItemID != "" {
		args = append(args, "item_id", e.ItemID)
	}
	if e.BatchID != "" {
		args = append(args, "batch_id", e.BatchID)
	}
	if e.Reason != "" {
		args = append(args, "reason", e.Reason)
	}
	if e.Items != 0 {
		args = append(args, "items", e.Items)
	}
	if e.Bytes != 0 {
		args = append(args, "bytes", e.Bytes)
	}
	if e.Err != "" {
		args = append(args, "err", e.Err)
	}
	s.L.Info("batch scheduler event", args...)
}
