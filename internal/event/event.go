// Package event defines the structured state-change record emitted by the
// scheduler. Every observable mutation (arrival, dispatch, preemption, lock
// hand-off, priority inheritance, deadlock, ...) produces an Event.
package event

import (
	"encoding/json"
	"sync"
)

// Kind enumerates the event types written to a timeline.
type Kind string

const (
	// TaskArrive: a task reached its arrival time and became ready.
	TaskArrive Kind = "task_arrive"
	// TaskDispatch: the scheduler chose a task to run on the processor.
	TaskDispatch Kind = "task_dispatch"
	// TaskWakeup: a blocked task had its lock request granted and became ready.
	TaskWakeup Kind = "task_wakeup"
	// TaskBlock: a task tried to acquire a held lock and blocked.
	TaskBlock Kind = "task_block"
	// TaskPreempted: a running task was replaced by a higher-priority task.
	TaskPreempted Kind = "task_preempted"
	// TaskExit: a task finished its program.
	TaskExit Kind = "task_exit"
	// LockAcquire: a lock became owned by a task (free lock taken).
	LockAcquire Kind = "lock_acquire"
	// LockRelease: a lock was released.
	LockRelease Kind = "lock_release"
	// LockGrant: a released lock was handed directly to a waiting task.
	LockGrant Kind = "lock_grant"
	// PriorityChange: a task's effective priority changed due to inheritance
	// or priority restoration after release.
	PriorityChange Kind = "priority_change"
	// Tick: one processor tick of execution was consumed.
	Tick Kind = "tick"
	// IdleJump: the processor had no ready task and time jumped to the next
	// arrival.
	IdleJump Kind = "idle_jump"
	// Deadlock: the wait-for graph contains a cycle; simulation halted.
	Deadlock Kind = "deadlock"
	// ProgramError: a task executed an illegal operation (unlock a lock it
	// does not own, ...); simulation halted.
	ProgramError Kind = "program_error"
)

// Event is one immutable record on the timeline. Detail carries
// kind-specific payload and is kept loosely typed for JSON flexibility;
// helpers below construct the common payloads.
type Event struct {
	// Seq is the dense, zero-based assignment order of the event.
	Seq int64 `json:"seq"`
	// Time is the logical tick at which the event occurred.
	Time int64 `json:"time"`
	// Kind is the event type.
	Kind Kind `json:"kind"`
	// Task is the primary task involved ("" when not applicable).
	Task string `json:"task,omitempty"`
	// Resource is the lock involved ("" when not applicable).
	Resource string `json:"resource,omitempty"`
	// Detail holds structured, kind-specific extra fields.
	Detail map[string]any `json:"detail,omitempty"`
}

// Sink consumes events. Emit must be safe to call from the simulation loop;
// implementations decide whether they are concurrency-safe.
type Sink interface {
	Emit(e Event)
}

// MemorySink stores every event in assignment order.
type MemorySink struct {
	mu     sync.Mutex
	events []Event
}

// NewMemorySink returns an empty in-memory sink.
func NewMemorySink() *MemorySink { return &MemorySink{} }

func (s *MemorySink) Emit(e Event) {
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

// JSON renders the events as a JSON document (indented when pretty is true).
func (s *MemorySink) JSON(pretty bool) ([]byte, error) {
	evs := s.Events()
	if evs == nil {
		evs = []Event{}
	}
	if pretty {
		return json.MarshalIndent(evs, "", "  ")
	}
	return json.Marshal(evs)
}

// ChannelSink forwards events onto a channel (useful for streaming
// executors); Emit blocks until the channel accepts the event.
type ChannelSink struct{ C chan Event }

// NewChannelSink wraps ch.
func NewChannelSink(ch chan Event) *ChannelSink { return &ChannelSink{C: ch} }

func (s *ChannelSink) Emit(e Event) { s.C <- e }

// MultiSink fans each event out to every wrapped sink.
type MultiSink []Sink

// Emit fans the event out to all wrapped sinks.
func (m MultiSink) Emit(e Event) {
	for _, s := range m {
		s.Emit(e)
	}
}
