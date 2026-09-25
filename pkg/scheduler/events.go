package scheduler

import (
	"bufio"
	"encoding/json"
	"io"
	"os"
	"sync"
)

// EventSink receives every structured state-change event. Implementations must
// be safe for concurrent use; the scheduler also holds its own mutex while
// calling Record.
type EventSink interface {
	Record(ev Event)
}

// MemorySink retains events in order. A cap of 0 means unbounded.
type MemorySink struct {
	mu     sync.Mutex
	events []Event
	cap    int
}

// NewMemorySink creates an in-memory sink holding at most cap events
// (unbounded when cap <= 0).
func NewMemorySink(cap int) *MemorySink { return &MemorySink{cap: cap} }

// Record appends ev, dropping the oldest event at capacity.
func (m *MemorySink) Record(ev Event) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.events = append(m.events, ev)
	if m.cap > 0 && len(m.events) > m.cap {
		m.events = m.events[len(m.events)-m.cap:]
	}
}

// Events returns a copy of the retained events.
func (m *MemorySink) Events() []Event {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]Event, len(m.events))
	copy(out, m.events)
	return out
}

// MultiSink fans every event out to several sinks.
type MultiSink struct{ sinks []EventSink }

// NewMultiSink wraps several sinks.
func NewMultiSink(sinks ...EventSink) *MultiSink { return &MultiSink{sinks: sinks} }

// Record forwards the event to every wrapped sink.
func (m *MultiSink) Record(ev Event) {
	for _, s := range m.sinks {
		s.Record(ev)
	}
}

// FileSink writes one JSON object per line (JSON Lines) to a writer, safe for
// concurrent use.
type FileSink struct {
	mu sync.Mutex
	w  io.Writer
	e  *json.Encoder
}

// NewFileSink writes JSONL to w.
func NewFileSink(w io.Writer) *FileSink {
	e := json.NewEncoder(w)
	e.SetEscapeHTML(false)
	return &FileSink{w: w, e: e}
}

// NewFileSinkFromPath opens (create/append) path and returns a sink plus a
// close function.
func NewFileSinkFromPath(path string) (*FileSink, func() error, error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return nil, nil, err
	}
	bw := bufio.NewWriter(f)
	sink := NewFileSink(bw)
	closeFn := func() error {
		if err := bw.Flush(); err != nil {
			_ = f.Close()
			return err
		}
		return f.Close()
	}
	return sink, closeFn, nil
}

// Record writes ev as one JSON line and flushes immediately (events must
// survive a crash and be tail-able).
func (f *FileSink) Record(ev Event) {
	f.mu.Lock()
	defer f.mu.Unlock()
	_ = f.e.Encode(ev)
	if bw, ok := f.w.(*bufio.Writer); ok {
		_ = bw.Flush()
	}
}
