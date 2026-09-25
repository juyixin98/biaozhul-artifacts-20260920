package scheduler

import (
	"encoding/json"
	"io"
	"sync"
	"sync/atomic"
)

// State is the lifecycle state of a task.
type State string

// Task lifecycle states.
const (
	StatePending   State = "pending"   // created, not yet running
	StateRunning   State = "running"   // function executing on some worker
	StateSucceeded State = "succeeded" // function returned without error
	StateFailed    State = "failed"    // function returned a non-nil error
	StatePanicked  State = "panicked"  // function panicked
	StateCanceled  State = "canceled"  // canceled before or during execution
)

// IsTerminal reports whether s is a terminal state.
func (s State) IsTerminal() bool {
	switch s {
	case StateSucceeded, StateFailed, StatePanicked, StateCanceled:
		return true
	}
	return false
}

// EventType enumerates the structured events emitted by an executor.
type EventType string

// All emitted event types.
const (
	EventSubmitted EventType = "task.submitted" // task accepted by Submit
	EventSpawned   EventType = "task.spawned"   // child task created from a running task
	EventStarted   EventType = "task.started"   // function begins executing
	EventCompleted EventType = "task.completed" // reached a terminal state
	EventCanceled  EventType = "task.canceled"  // Cancel() observed by the executor
	EventStole     EventType = "task.stolen"    // a worker stole a task from another deque
	EventShutdown  EventType = "executor.shutdown"
)

// Event is one structured state-change record. Timestamps come from
// the executor's clock, so tests using a MockClock see deterministic
// values. Seq is a process-wide monotonically increasing counter
// assigned under the executor lock, hence consistent with happens-before
// ordering of emissions.
type Event struct {
	Seq      int64          `json:"seq"`
	Time     string         `json:"time"`
	Type     EventType      `json:"type"`
	TaskID   string         `json:"task_id,omitempty"`
	ParentID string         `json:"parent_id,omitempty"`
	Kind     string         `json:"kind,omitempty"`
	State    State          `json:"state,omitempty"`
	Worker   int            `json:"worker,omitempty"`
	From     int            `json:"from,omitempty"` // victim worker on steal
	Err      string         `json:"err,omitempty"`
	Detail   map[string]any `json:"detail,omitempty"`
}

// EventSink receives every emitted event. Implementations must be
// safe for concurrent use.
type EventSink interface {
	Record(Event)
}

// NopSink drops all events.
type NopSink struct{}

// Record implements EventSink.
func (NopSink) Record(Event) {}

// ChannelSink forwards every event onto a channel. The channel must
// have capacity adequate for the burst; Record blocks if it is full,
// providing natural back-pressure.
type ChannelSink struct{ Ch chan Event }

// NewChannelSink creates a ChannelSink with the given buffer size.
func NewChannelSink(buffer int) *ChannelSink {
	return &ChannelSink{Ch: make(chan Event, buffer)}
}

// Record implements EventSink.
func (s *ChannelSink) Record(e Event) { s.Ch <- e }

// JSONSink writes one JSON object per line (JSONL) to w. Writes are
// serialized. It never returns errors from Record: a write error is
// stored and recoverable via Err(), so the executor never blocks on a
// broken sink.
type JSONSink struct {
	mu  sync.Mutex
	w   io.Writer
	e   *json.Encoder
	err error
}

// NewJSONSink writes JSON Lines to w.
func NewJSONSink(w io.Writer) *JSONSink {
	s := &JSONSink{w: w}
	s.e = json.NewEncoder(w)
	s.e.SetEscapeHTML(false)
	return s
}

// Record implements EventSink.
func (s *JSONSink) Record(ev Event) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.err != nil {
		return
	}
	s.err = s.e.Encode(ev)
}

// Err reports the first encoding/write error encountered, if any.
func (s *JSONSink) Err() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.err
}

// MemorySink keeps the most recent capacity events in a ring buffer
// and supports snapshot reads by sequence number. It is safe for
// concurrent use and suitable for the HTTP event endpoint.
type MemorySink struct {
	mu       sync.Mutex
	ring     []Event
	capacity int
	next     int // index of the oldest slot
	size     int
	lastSeq  int64
}

// NewMemorySink creates a ring-buffer sink holding at most capacity
// events (capacity < 1 is treated as 1).
func NewMemorySink(capacity int) *MemorySink {
	if capacity < 1 {
		capacity = 1
	}
	return &MemorySink{ring: make([]Event, capacity), capacity: capacity}
}

// Record implements EventSink.
func (s *MemorySink) Record(e Event) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.size == s.capacity {
		s.next = (s.next + 1) % s.capacity
	} else {
		s.size++
	}
	s.ring[(s.next+s.size-1)%s.capacity] = e
	s.lastSeq = e.Seq
}

// Since returns retained events with Seq > afterSeq, in order, along
// with the last sequence number currently stored.
func (s *MemorySink) Since(afterSeq int64) (events []Event, lastSeq int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	events = make([]Event, 0, s.size)
	for i := 0; i < s.size; i++ {
		e := s.ring[(s.next+i)%s.capacity]
		if e.Seq > afterSeq {
			events = append(events, e)
		}
	}
	return events, s.lastSeq
}

// Len returns the number of currently retained events.
func (s *MemorySink) Len() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.size
}

// fanout fans each event out to all configured sinks. Delivery is
// synchronous and the executor lock is held for the duration, so
// delivery order is exactly the process-wide sequence order and a
// task becoming terminal is always preceded by the visibility of its
// events (strong happens-before for callers reading sinks after
// Result()/Wait()). A slow sink therefore applies back-pressure to
// the scheduler; the built-in sinks (memory ring, JSON, buffered
// channel) are non-blocking in the common case.
type fanout struct {
	sinks []EventSink
}

func newFanout(sinks ...EventSink) *fanout {
	alive := sinks[:0]
	for _, s := range sinks {
		if s != nil {
			alive = append(alive, s)
		}
	}
	return &fanout{sinks: alive}
}

// Record delivers the event synchronously to every sink.
func (f *fanout) Record(e Event) {
	for _, s := range f.sinks {
		s.Record(e)
	}
}

// close is a no-op for synchronous delivery (kept as a lifecycle
// hook for future/async sink implementations).
func (f *fanout) close() {}

// seqSource hands out process-wide increasing event sequence numbers.
type seqSource struct{ n atomic.Int64 }

func (s *seqSource) next() int64 { return s.n.Add(1) }
