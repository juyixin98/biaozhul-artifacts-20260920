package pool

import (
	"encoding/json"
	"io"
	"sync"
	"time"
)

// EventKind enumerates the structured state transitions emitted by a Pool.
type EventKind string

const (
	// EventPoolCreated marks pool construction.
	EventPoolCreated EventKind = "pool.created"
	// EventTaskSubmitted is emitted once a task has been accepted.
	EventTaskSubmitted EventKind = "task.submitted"
	// EventTaskRejected is emitted when a submission is turned away.
	EventTaskRejected EventKind = "task.rejected"
	// EventTaskScheduled is emitted when a delayed task becomes queued.
	EventTaskScheduled EventKind = "task.scheduled"
	// EventTaskStarted marks the instant a worker begins running a task.
	EventTaskStarted EventKind = "task.started"
	// EventTaskCompleted marks normal completion (value settled).
	EventTaskCompleted EventKind = "task.completed"
	// EventTaskFailed marks a task whose function returned an error or panicked.
	EventTaskFailed EventKind = "task.failed"
	// EventTaskCanceled marks a pending task canceled before it could run.
	EventTaskCanceled EventKind = "task.canceled"
	// EventWorkerStarted marks a worker goroutine entering its loop.
	EventWorkerStarted EventKind = "worker.started"
	// EventWorkerStopped marks a worker goroutine exiting (retire or drain).
	EventWorkerStopped EventKind = "worker.stopped"
	// EventPoolResizing marks a change in the desired worker count.
	EventPoolResizing EventKind = "pool.resizing"
	// EventPoolShutdown marks graceful shutdown begin.
	EventPoolShutdown EventKind = "pool.shutdown"
	// EventPoolTerminated marks the terminal state (fully drained).
	EventPoolTerminated EventKind = "pool.terminated"
	// EventPoolForceCanceled marks ShutdownNow begin.
	EventPoolForceCanceled EventKind = "pool.force_canceled"
)

// Event is an immutable structured record of one state transition.
type Event struct {
	Time      time.Time      `json:"time"`
	Kind      EventKind      `json:"kind"`
	Pool      string         `json:"pool"`
	WorkerID  int64          `json:"worker_id,omitempty"`
	TaskID    string         `json:"task_id,omitempty"`
	Current   int            `json:"current,omitempty"`
	Desired   int            `json:"desired,omitempty"`
	QueueLen  int            `json:"queue_len,omitempty"`
	QueueCap  int            `json:"queue_cap,omitempty"`
	Completed int64          `json:"completed,omitempty"`
	Reason    string         `json:"reason,omitempty"`
	Err       string         `json:"error,omitempty"`
	Extra     map[string]any `json:"extra,omitempty"`
}

// Sink receives events. Implementations must be safe for concurrent use.
type Sink interface {
	Emit(Event)
}

// SinkFunc adapts a function into a Sink.
type SinkFunc func(Event)

// Emit implements Sink.
func (f SinkFunc) Emit(e Event) { f(e) }

// NopSink drops every event.
var NopSink Sink = SinkFunc(func(Event) {})

// EventRecorder is the default Sink: it keeps the newest ringSize events and
// fans every event out to subscribers in parallel. Emission never blocks the
// pool: a slow subscriber with a full channel is skipped (with a dropped
// counter per subscriber when using Subscribe).
type EventRecorder struct {
	mu          sync.Mutex
	ring        []Event
	ringSize    int
	next        int
	filled      bool
	subscribers map[int64]chan Event
	subSeq      int64
}

// NewEventRecorder creates an EventRecorder retaining the newest ringSize
// events. ringSize <= 0 disables retention (fan-out still works).
func NewEventRecorder(ringSize int) *EventRecorder {
	return &EventRecorder{
		ringSize:    ringSize,
		subscribers: make(map[int64]chan Event),
	}
}

// Emit stores the event in the ring and fans it out to subscribers.
func (r *EventRecorder) Emit(e Event) {
	r.mu.Lock()
	if r.ringSize > 0 {
		if len(r.ring) < r.ringSize {
			r.ring = append(r.ring, e)
		} else {
			r.ring[r.next] = e
			r.next = (r.next + 1) % r.ringSize
		}
	}
	subs := r.subscribers
	chans := make([]chan Event, 0, len(subs))
	for _, ch := range subs {
		chans = append(chans, ch)
	}
	r.mu.Unlock()
	for _, ch := range chans {
		select {
		case ch <- e:
		default:
			// Slow subscriber: drop rather than block the pool.
		}
	}
}

// Events returns retained events in chronological order.
func (r *EventRecorder) Events() []Event {
	r.mu.Lock()
	defer r.mu.Unlock()
	if len(r.ring) < r.ringSize || r.ringSize == 0 {
		out := make([]Event, len(r.ring))
		copy(out, r.ring)
		return out
	}
	out := make([]Event, 0, r.ringSize)
	out = append(out, r.ring[r.next:]...)
	out = append(out, r.ring[:r.next]...)
	return out
}

// Subscribe returns a buffered channel of events and a cancel function.
// Events that do not fit the buffer are dropped. A backlog of the retained
// ring (from oldest to newest) is delivered first.
func (r *EventRecorder) Subscribe(buffer int) (<-chan Event, func()) {
	if buffer < 1 {
		buffer = 1
	}
	ch := make(chan Event, buffer)
	r.mu.Lock()
	r.subSeq++
	id := r.subSeq
	r.subscribers[id] = ch
	backlog := make([]Event, 0, r.ringSize)
	if r.ringSize > 0 {
		if len(r.ring) < r.ringSize {
			backlog = append(backlog, r.ring...)
		} else {
			backlog = append(backlog, r.ring[r.next:]...)
			backlog = append(backlog, r.ring[:r.next]...)
		}
	}
	r.mu.Unlock()
	for _, e := range backlog {
		select {
		case ch <- e:
		default:
		}
	}
	cancel := func() {
		r.mu.Lock()
		delete(r.subscribers, id)
		r.mu.Unlock()
	}
	return ch, cancel
}

// JSONSink writes one JSON object per event to w, each followed by '\n'.
type JSONSink struct {
	mu sync.Mutex
	w  io.Writer
}

// NewJSONSink wraps w as a Sink emitting newline-delimited JSON.
func NewJSONSink(w io.Writer) *JSONSink { return &JSONSink{w: w} }

// Emit implements Sink.
func (s *JSONSink) Emit(e Event) {
	b, err := json.Marshal(e)
	if err != nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	_, _ = s.w.Write(b)
	_, _ = s.w.Write([]byte{'\n'})
}

// multiSink fans an event out to every wrapped sink.
type multiSink struct{ sinks []Sink }

// MultiSink returns a Sink that emits to all provided sinks.
func MultiSink(sinks ...Sink) Sink { return &multiSink{sinks: sinks} }

func (m *multiSink) Emit(e Event) {
	for _, s := range m.sinks {
		s.Emit(e)
	}
}
