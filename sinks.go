package dynpool

import (
	"encoding/json"
	"io"
	"sync"
)

// MemorySink retains every emitted event in order. Safe for concurrent use.
type MemorySink struct {
	mu     sync.Mutex
	events []Event
}

// OnEvent implements Sink.
func (s *MemorySink) OnEvent(e Event) {
	s.mu.Lock()
	s.events = append(s.events, e)
	s.mu.Unlock()
}

// Events returns a copy of the recorded events.
func (s *MemorySink) Events() []Event {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]Event, len(s.events))
	copy(out, s.events)
	return out
}

// Len returns the number of recorded events.
func (s *MemorySink) Len() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.events)
}

// Types returns event types in recorded order.
func (s *MemorySink) Types() []EventType {
	evs := s.Events()
	out := make([]EventType, len(evs))
	for i, e := range evs {
		out[i] = e.Type
	}
	return out
}

// Reset clears recorded events.
func (s *MemorySink) Reset() {
	s.mu.Lock()
	s.events = nil
	s.mu.Unlock()
}

// MultiSink fans every event out to the given sinks in order.
func MultiSink(sinks ...Sink) Sink {
	return SinkFunc(func(e Event) {
		for _, s := range sinks {
			s.OnEvent(e)
		}
	})
}

// JSONSink writes each event as one JSON line to w. All writes are serialized.
type JSONSink struct {
	mu sync.Mutex
	w  io.Writer
}

// NewJSONSink creates a JSON-lines event sink.
func NewJSONSink(w io.Writer) *JSONSink { return &JSONSink{w: w} }

// OnEvent implements Sink. Errors from the writer are ignored (events must
// never break the pool); callers that need error handling should wrap a writer.
func (s *JSONSink) OnEvent(e Event) {
	b, err := json.Marshal(e)
	if err != nil {
		return
	}
	b = append(b, '\n')
	s.mu.Lock()
	_, _ = s.w.Write(b)
	s.mu.Unlock()
}
