package scheduler

import (
	"sync"
	"time"
)

// Wait blocks until the job reaches a terminal state.
func (j *Job) Wait() {
	<-j.done
}

// Done returns a channel closed when the job reaches a terminal state.
func (j *Job) Done() <-chan struct{} { return j.done }

// Cancel requests cancellation. Returns false if already terminal.
func (j *Job) Cancel() bool {
	j.mu.Lock()
	if j.state != JobRunning {
		j.mu.Unlock()
		return false
	}
	j.cancelRequested = true
	j.mu.Unlock()
	// Signals never hold scheduler locks.
	j.cancelOnce.Do(func() {
		j.cancel()
		close(j.cancelCh)
	})
	return true
}

// State returns the current job state.
func (j *Job) State() JobState {
	j.mu.Lock()
	defer j.mu.Unlock()
	return j.state
}

// Name returns the spec-provided job name.
func (j *Job) Name() string { return j.spec.Name }

// JobSnapshot is a point-in-time, concurrency-safe view of a job.
type JobSnapshot struct {
	ID        string       `json:"id"`
	Name      string       `json:"name"`
	State     JobState     `json:"state"`
	Nodes     []NodeStatus `json:"nodes"`
	CreatedAt time.Time    `json:"createdAt"`
	EndedAt   time.Time    `json:"endedAt,omitempty"`
}

// Snapshot returns the current state of the job and all its nodes.
func (j *Job) Snapshot() JobSnapshot {
	j.mu.Lock()
	defer j.mu.Unlock()
	snap := JobSnapshot{ID: j.ID, Name: j.spec.Name, State: j.state, CreatedAt: j.createdAt, EndedAt: j.endedAt}
	// Emit in spec order for stable output.
	for _, n := range j.spec.Nodes {
		rt := j.states[n.Name]
		deps := make([]string, 0, len(n.Deps))
		deps = append(deps, n.Deps...)
		status := NodeStatus{
			Name:        n.Name,
			State:       rt.state,
			Attempts:    rt.attempts,
			MaxAttempts: rt.maxAttempts,
			Policy:      rt.policy,
			Deps:        deps,
			LastError:   rt.lastErr,
			StartedAt:   rt.startedAt,
			EndedAt:     rt.endedAt,
		}
		snap.Nodes = append(snap.Nodes, status)
	}
	return snap
}

// SliceSink collects events in memory. It is safe for concurrent use and
// suitable for tests and the HTTP event log.
type SliceSink struct {
	mu     sync.Mutex
	events []Event
	limit  int
}

// NewSliceSink creates an unbounded in-memory sink.
func NewSliceSink() *SliceSink { return &SliceSink{} }

// NewBoundedSliceSink keeps at most limit most-recent events.
func NewBoundedSliceSink(limit int) *SliceSink { return &SliceSink{limit: limit} }

// Record implements Sink.
func (s *SliceSink) Record(e Event) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.events = append(s.events, e)
	if s.limit > 0 && len(s.events) > s.limit {
		s.events = s.events[len(s.events)-s.limit:]
	}
}

// Events returns a copy of the recorded events in emission order.
func (s *SliceSink) Events() []Event {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]Event(nil), s.events...)
}

// EventsFor returns a copy of events belonging to one job, in order.
func (s *SliceSink) EventsFor(jobID string) []Event {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []Event
	for _, e := range s.events {
		if e.JobID == jobID {
			out = append(out, e)
		}
	}
	return out
}
