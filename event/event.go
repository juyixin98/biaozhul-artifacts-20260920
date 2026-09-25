// Package event provides structured, append-only records of scheduler state
// changes. Every admission decision, enqueue, start, completion, miss and
// cancellation produces one Event; sinks consume them without feeding back
// into scheduling decisions.
package event

import (
	"encoding/json"
	"io"
	"sync"
	"time"
)

// Type enumerates the state changes that produce events.
type Type string

const (
	// Submitted marks arrival of a job that passed request validation;
	// Reason describes the admission decision that followed.
	Submitted Type = "submitted"
	// Admitted means the job entered the queue (it was not started at once).
	Admitted Type = "admitted"
	// Rejected means conservative admission refused the job (predicted
	// infeasible); the job holds no resources and never runs.
	Rejected Type = "rejected"
	// Started marks dispatch: resources were acquired and the executor began.
	Started Type = "started"
	// Completed means the executor returned success before the deadline.
	Completed Type = "completed"
	// DeadlineMissed means a queued job passed its deadline, or (only with
	// OverrunPolicyObserve) a running job finished after its deadline.
	DeadlineMissed Type = "deadline_missed"
	// Timeout means a running job exceeded its declared execution upper bound
	// and was killed (only with OverrunPolicyKillAtBudget).
	Timeout Type = "timeout"
	// Canceled means a cancel request terminated a queued or running job.
	Canceled Type = "canceled"
	// Failed means the executor reported an execution error before the
	// deadline; resources are released exactly as for completion.
	Failed Type = "failed"
)

// Event is one structured state-change record. Times are integer Unix
// milliseconds so records stay comparable and hand-verifiable in demos and
// tests.
type Event struct {
	Seq    int64          `json:"seq"`
	TimeMs int64          `json:"time_ms"`
	Type   Type           `json:"type"`
	JobID  string         `json:"job_id"`
	Reason string         `json:"reason,omitempty"`
	Detail map[string]any `json:"detail,omitempty"`
}

// Sink consumes events. Implementations must be safe for concurrent use.
type Sink interface {
	Write(e Event)
}

// MemorySink retains events in memory (bounded). When capacity is exceeded the
// oldest events are dropped. It is safe for concurrent use.
type MemorySink struct {
	mu       sync.Mutex
	events   []Event
	capacity int
	dropped  int
}

// NewMemorySink creates a sink holding at most capacity events (0 => 10000).
func NewMemorySink(capacity int) *MemorySink {
	if capacity <= 0 {
		capacity = 10000
	}
	return &MemorySink{capacity: capacity}
}

func (s *MemorySink) Write(e Event) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.events = append(s.events, e)
	if len(s.events) > s.capacity {
		n := len(s.events) - s.capacity
		s.events = s.events[n:]
		s.dropped += n
	}
}

// Events returns a copy of the retained events in order.
func (s *MemorySink) Events() []Event {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]Event, len(s.events))
	copy(out, s.events)
	return out
}

// Dropped reports how many events were discarded due to capacity.
func (s *MemorySink) Dropped() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.dropped
}

// Reset clears retained events.
func (s *MemorySink) Reset() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.events = nil
	s.dropped = 0
}

// JSONLSink writes one JSON object per event line to an io.Writer. Safe for
// concurrent use as long as concurrent Write calls to the writer are allowed
// (an *os.File or a mutex-guarded writer).
type JSONLSink struct {
	mu sync.Mutex
	w  io.Writer
}

// NewJSONLSink creates a JSON Lines sink.
func NewJSONLSink(w io.Writer) *JSONLSink { return &JSONLSink{w: w} }

func (s *JSONLSink) Write(e Event) {
	b, err := json.Marshal(e)
	if err != nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	_, _ = s.w.Write(append(b, '\n'))
}

// Multi fans every event out to multiple sinks.
func Multi(sinks ...Sink) Sink { return &multi{sinks: sinks} }

type multi struct{ sinks []Sink }

func (m *multi) Write(e Event) {
	for _, s := range m.sinks {
		s.Write(e)
	}
}

// Clock is the subset of time source the event recorder needs.
type Clock interface {
	Now() time.Time
}

// Recorder stamps events with a sequence number and the clock's time.
type Recorder struct {
	clock Clock
	sink  Sink
	seq   int64
	mu    sync.Mutex
}

// NewRecorder builds a recorder over sink.
func NewRecorder(c Clock, sink Sink) *Recorder { return &Recorder{clock: c, sink: sink} }

// Emit records an event, stamping Seq and TimeMs. The caller supplies type,
// job id, reason and detail.
func (r *Recorder) Emit(t Type, jobID, reason string, detail map[string]any) Event {
	r.mu.Lock()
	r.seq++
	seq := r.seq
	now := r.clock.Now()
	r.mu.Unlock()

	e := Event{
		Seq:    seq,
		TimeMs: now.UnixMilli(),
		Type:   t,
		JobID:  jobID,
		Reason: reason,
		Detail: detail,
	}
	r.sink.Write(e)
	return e
}
