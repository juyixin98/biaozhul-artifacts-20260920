// Package event defines the structured state-change records emitted by the
// budget layer and an append-only bus that fans them out to sinks.
package event

import (
	"encoding/json"
	"fmt"
	"io"
	"sync"
	"sync/atomic"

	"tokenbudget/internal/clock"
)

// Kind enumerates every discrete state change the budget layer can record.
type Kind string

const (
	KindTenantCreated Kind = "tenant_created"
	KindGranted       Kind = "granted"        // tokens consumed
	KindDenied        Kind = "denied"         // request rejected; nothing consumed
	KindReserved      Kind = "reserved"       // tokens pre-consumed for a queued job
	KindConfigChanged Kind = "config_changed" // rate/capacity replaced
	KindJobQueued     Kind = "job_queued"
	KindJobExecuted   Kind = "job_executed"
	KindJobRejected   Kind = "job_rejected"
)

// Scope identifies which bucket layer an event concerns.
type Scope string

const (
	ScopeGlobal Scope = "global"
	ScopeTenant Scope = "tenant"
	ScopeSystem Scope = "system" // scheduler / non-bucket events
)

// Detail carries structured payload fields. All pointer fields are omitted
// from JSON when not relevant, so each event contains exactly its payload.
type Detail struct {
	// Scope/Token/Remaining/Reason describe grant, deny and reserve events,
	// for both layers of a two-layer attempt (one event per affected layer).
	Scope     Scope    `json:"scope,omitempty"`
	Tenant    string   `json:"tenant,omitempty"`
	Tokens    int64    `json:"tokens,omitempty"` // always > 0 on grant/deny/reserve
	Remaining int64    `json:"remaining,omitempty"`
	Reason    string   `json:"reason,omitempty"`
	WaitNS    int64    `json:"wait_ns,omitempty"` // wait required (deny) or taken (reserve)
	JobID     string   `json:"job_id,omitempty"`
	Name      string   `json:"name,omitempty"` // config slot or job name
	Before    *Summary `json:"before,omitempty"`
	After     *Summary `json:"after,omitempty"`
}

// Summary is a self-contained rate-limit configuration snapshot.
type Summary struct {
	RateNum  int64 `json:"rate_num"`
	RateDen  int64 `json:"rate_den_ns"`
	Capacity int64 `json:"capacity"`
	// Available is clamped; available+frac gives the exact stock.
	Available int64 `json:"available"`
	FracNS    int64 `json:"frac_ns"` // available is exact when frac_ns == 0
}

// Event is one immutable structured record.
type Event struct {
	Seq    uint64 `json:"seq"`
	AtNS   int64  `json:"at_ns"` // monotonic instant
	Kind   Kind   `json:"kind"`
	Detail Detail `json:"detail"`
}

// Sink receives events in global sequence order. Implementations must not
// block for long while holding the bus mutex; busSerialSink wraps blocking
// sinks with an async queue.
type Sink interface {
	Write(Event)
}

// Bus assigns sequence numbers and timestamps and fans events out.
type Bus interface {
	Emit(kind Kind, d Detail)
}

// MemorySink keeps every event in memory; it never blocks.
type MemorySink struct {
	mu     sync.Mutex
	events []Event
}

// Write appends one event.
func (s *MemorySink) Write(e Event) {
	s.mu.Lock()
	s.events = append(s.events, e)
	s.mu.Unlock()
}

// Events returns a copy of all recorded events.
func (s *MemorySink) Events() []Event {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]Event, len(s.events))
	copy(out, s.events)
	return out
}

// Len reports the number of recorded events.
func (s *MemorySink) Len() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.events)
}

// JSONSink writes one JSON object per line to w (JSON Lines). Writes are
// serialized; a mutex protects writers that share the underlying io.Writer.
type JSONSink struct {
	mu sync.Mutex
	w  io.Writer
}

// NewJSONSink creates a JSONL sink.
func NewJSONSink(w io.Writer) *JSONSink { return &JSONSink{w: w} }

// Write appends e as a single JSON line.
func (s *JSONSink) Write(e Event) {
	b, err := json.Marshal(e)
	if err != nil { // Event is marshalable by construction.
		b = []byte(fmt.Sprintf(`{"kind":"%s","marshal_error":true}`, e.Kind))
	}
	s.mu.Lock()
	_, _ = s.w.Write(append(b, '\n'))
	s.mu.Unlock()
}

// serialSink decouples a slow sink from the emitter via an unbounded queue.
type serialSink struct {
	in chan Event
}

// NewSerialSink wraps any sink so the bus never blocks on it. Events are
// delivered to the wrapped sink on a dedicated goroutine in emission order.
func NewSerialSink(inner Sink) Sink {
	s := &serialSink{in: make(chan Event, 1024)}
	go func() {
		for e := range s.in {
			inner.Write(e)
		}
	}()
	return s
}

func (s *serialSink) Write(e Event) { s.in <- e }

// bus is the default Bus implementation.
type bus struct {
	clk   clock.Clock
	seq   atomic.Uint64
	mu    sync.Mutex
	sinks []Sink
}

// NewBus builds a bus timestamped by clk and fanning out to sinks.
// Sinks implementing no extra interface are wrapped asynchronously when they
// are not already non-blocking; pass only *MemorySink / *JSONSink or supply
// your own (NewBus wraps unknown sinks with a serial queue).
func NewBus(clk clock.Clock, sinks ...Sink) Bus {
	b := &bus{clk: clk, sinks: make([]Sink, 0, len(sinks))}
	for _, s := range sinks {
		switch s.(type) {
		case *MemorySink, *JSONSink:
			b.sinks = append(b.sinks, s)
		default:
			b.sinks = append(b.sinks, NewSerialSink(s))
		}
	}
	return b
}

// Emit stamps, sequences and dispatches one event to every sink.
func (b *bus) Emit(kind Kind, d Detail) {
	e := Event{
		Seq:    b.seq.Add(1),
		AtNS:   int64(b.clk.Now()),
		Kind:   kind,
		Detail: d,
	}
	b.mu.Lock()
	sinks := b.sinks
	b.mu.Unlock()
	for _, s := range sinks {
		s.Write(e)
	}
}
