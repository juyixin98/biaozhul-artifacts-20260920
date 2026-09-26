// Package breaker implements a circuit breaker tripped by a sliding window of
// call samples, with a bounded number of half-open probe permits.
//
// The concurrency contract that the tests pin down:
//
//   - Every state transition bumps an opaque Generation. A permit carries the
//     generation it was acquired under. Reporting a result under a stale
//     generation (the call started in CLOSED or an earlier HALF_OPEN round)
//     never mutates the current generation's counters or state.
//   - HALF_OPEN admits at most MaxProbeCalls concurrent probes; extra callers
//     are rejected with ErrOpen while the slots are occupied.
//   - A context cancel/timeout is reported as OutcomeCanceled, never as
//     OutcomeFailure, so canceling an in-flight call cannot trip the breaker
//     or fail a half-open probe.
package breaker

import (
	"errors"
	"sync"
	"time"
)

// State of the breaker.
type State string

const (
	StateClosed   State = "closed"
	StateOpen     State = "open"
	StateHalfOpen State = "half_open"
)

// Outcome of a permitted call.
type Outcome string

const (
	OutcomeSuccess  Outcome = "success"
	OutcomeFailure  Outcome = "failure"
	OutcomeCanceled Outcome = "canceled"
)

// ErrOpen is returned by Allow when no call may proceed.
var ErrOpen = errors.New("circuit breaker is open")

// Config configures the breaker.
type Config struct {
	// WindowSize is the number of the most recent call samples inspected when
	// deciding to trip (sliding sample window).
	WindowSize int
	// FailureThreshold trips the breaker when failures among the most recent
	// WindowSize samples reach this number.
	FailureThreshold int
	// OpenCoolDown is how long the breaker stays OPEN before allowing probes.
	OpenCoolDown time.Duration
	// MaxProbeCalls bounds concurrent calls in HALF_OPEN.
	MaxProbeCalls int
	// HalfOpenSuccessThreshold consecutive probe successes close the breaker.
	// Zero means MaxProbeCalls.
	HalfOpenSuccessThreshold int
}

func (c Config) validated() Config {
	if c.WindowSize <= 0 {
		c.WindowSize = 10
	}
	if c.FailureThreshold <= 0 {
		c.FailureThreshold = 5
	}
	if c.FailureThreshold > c.WindowSize {
		c.FailureThreshold = c.WindowSize
	}
	if c.OpenCoolDown <= 0 {
		c.OpenCoolDown = 5 * time.Second
	}
	if c.MaxProbeCalls <= 0 {
		c.MaxProbeCalls = 1
	}
	if c.HalfOpenSuccessThreshold <= 0 {
		c.HalfOpenSuccessThreshold = c.MaxProbeCalls
	}
	return c
}

// Snapshot is a structured, point-in-time view of breaker state. The scenario
// engine records snapshots to build reproducible test reports.
type Snapshot struct {
	State            State     `json:"state"`
	Generation       uint64    `json:"generation"`
	WindowTotal      int       `json:"window_total"`
	WindowSuccesses  int       `json:"window_successes"`
	WindowFailures   int       `json:"window_failures"`
	WindowCanceled   int       `json:"window_canceled"`
	InFlight         int       `json:"in_flight"`
	ProbePermitsFree int       `json:"probe_permits_free"`
	ProbeSuccesses   int       `json:"probe_successes"` // consecutive, this generation
	OpenedAt         time.Time `json:"opened_at,omitempty"`
	OpenDeadline     time.Time `json:"open_deadline,omitempty"`
	TotalCalls       uint64    `json:"total_calls"`
	TotalSuccesses   uint64    `json:"total_successes"`
	TotalFailures    uint64    `json:"total_failures"`
	TotalCanceled    uint64    `json:"total_canceled"`
	TotalRejected    uint64    `json:"total_rejected"`
}

// Permit is returned by Allow and must be completed exactly once with Done.
type Permit struct {
	b          *Breaker
	generation uint64
	probe      bool
	done       bool
}

// Done reports the call outcome. It is safe to call once; subsequent calls are
// ignored. A stale permit (older generation) has no effect on current state.
func (p *Permit) Done(outcome Outcome) {
	p.b.report(p, outcome)
}

// Generation is the generation under which this permit was acquired.
func (p *Permit) Generation() uint64 { return p.generation }

// IsProbe reports whether this permit consumed a HALF_OPEN probe slot.
func (p *Permit) IsProbe() bool { return p.probe }

// Breaker — all state behind one mutex; critical sections are O(WindowSize).
type Breaker struct {
	cfg Config
	clk clockReader

	mu sync.Mutex

	state      State
	generation uint64

	// CLOSED sliding sample window (ring).
	samples []Outcome
	idx     int // next write position
	filled  int

	inFlight int

	// OPEN bookkeeping.
	openedAt     time.Time
	openDeadline time.Time

	// HALF_OPEN bookkeeping.
	probesInFlight    int
	probeSuccessesRun int // consecutive successes in the current HALF_OPEN generation
	probeSeenFailure  bool

	// Lifetime counters.
	totalCalls     uint64
	totalSuccesses uint64
	totalFailures  uint64
	totalCanceled  uint64
	totalRejected  uint64
}

type clockReader interface {
	Now() time.Time
}

// New constructs a breaker.
func New(cfg Config, clk clockReader) *Breaker {
	cfg = cfg.validated()
	return &Breaker{
		cfg:     cfg,
		clk:     clk,
		state:   StateClosed,
		samples: make([]Outcome, cfg.WindowSize),
	}
}

// State returns the current state without blocking on a permit decision.
func (b *Breaker) State() State {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.possiblyEnterHalfOpenLocked()
	return b.state
}

// Generation returns the current generation counter.
func (b *Breaker) Generation() uint64 {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.generation
}

// Allow acquires permission to make a call. In CLOSED it always succeeds. In
// OPEN it either transitions to HALF_OPEN (cool-down elapsed) or rejects with
// ErrOpen. In HALF_OPEN it grants a free probe slot or rejects.
func (b *Breaker) Allow() (*Permit, error) {
	b.mu.Lock()
	defer b.mu.Unlock()

	b.possiblyEnterHalfOpenLocked()

	switch b.state {
	case StateClosed:
		b.inFlight++
		b.totalCalls++
		return &Permit{b: b, generation: b.generation, probe: false}, nil
	case StateHalfOpen:
		if b.probesInFlight >= b.cfg.MaxProbeCalls {
			b.totalRejected++
			return nil, ErrOpen
		}
		b.probesInFlight++
		b.inFlight++
		b.totalCalls++
		return &Permit{b: b, generation: b.generation, probe: true}, nil
	default: // OPEN
		b.totalRejected++
		return nil, ErrOpen
	}
}

// possiblyEnterHalfOpenLocked performs OPEN -> HALF_OPEN when due. Caller holds
// b.mu. (Time is read here rather than on a timer so transitions are
// deterministic under the virtual clock: an Allow or Snapshot observes it.)
func (b *Breaker) possiblyEnterHalfOpenLocked() {
	if b.state != StateOpen {
		return
	}
	if b.clk.Now().Before(b.openDeadline) {
		return
	}
	b.state = StateHalfOpen
	b.generation++
	b.probesInFlight = 0
	b.probeSuccessesRun = 0
	b.probeSeenFailure = false
}

func (b *Breaker) report(p *Permit, outcome Outcome) {
	b.mu.Lock()
	defer b.mu.Unlock()

	if p.done {
		return
	}
	p.done = true
	b.inFlight--

	// Stale generation: this call belongs to a previous state incarnation
	// (e.g. a CLOSED call still in flight when the breaker tripped, or a
	// HALF_OPEN probe from a probe round that already ended). It frees no
	// current-generation probe slot and mutates no transition counters; it is
	// only recorded in the lifetime totals for observability.
	if p.generation != b.generation {
		switch outcome {
		case OutcomeSuccess:
			b.totalSuccesses++
		case OutcomeFailure:
			b.totalFailures++
		case OutcomeCanceled:
			b.totalCanceled++
		}
		return
	}

	if p.probe {
		b.probesInFlight--
	}

	switch outcome {
	case OutcomeSuccess:
		b.totalSuccesses++
	case OutcomeFailure:
		b.totalFailures++
	case OutcomeCanceled:
		b.totalCanceled++
	}

	if p.probe {
		b.reportHalfOpenLocked(outcome)
	} else if b.state == StateClosed {
		b.recordSampleLocked(outcome)
		if outcome == OutcomeFailure && b.windowFailuresLocked() >= b.cfg.FailureThreshold {
			b.enterOpenLocked()
		}
	}
	// A non-probe permit reporting while state is already OPEN/HALF_OPEN can
	// only be a same-generation late callback of a transition that did not
	// bump (none such) — handled by the stale check in practice; nothing to do.
}

func (b *Breaker) reportHalfOpenLocked(outcome Outcome) {
	// Cancellation is not a probe verdict: the slot frees, the consecutive
	// success run is untouched and the breaker stays HALF_OPEN.
	switch outcome {
	case OutcomeFailure:
		b.probeSeenFailure = true
		b.enterOpenLocked()
	case OutcomeSuccess:
		b.probeSuccessesRun++
		if b.probeSuccessesRun >= b.cfg.HalfOpenSuccessThreshold {
			b.enterClosedLocked()
		}
	}
}

func (b *Breaker) recordSampleLocked(o Outcome) {
	b.samples[b.idx] = o
	b.idx = (b.idx + 1) % len(b.samples)
	if b.filled < len(b.samples) {
		b.filled++
	}
}

func (b *Breaker) windowFailuresLocked() int {
	n := 0
	for i := 0; i < b.filled; i++ {
		if b.samples[i] == OutcomeFailure {
			n++
		}
	}
	return n
}

func (b *Breaker) enterOpenLocked() {
	b.state = StateOpen
	b.generation++
	now := b.clk.Now()
	b.openedAt = now
	b.openDeadline = now.Add(b.cfg.OpenCoolDown)
	// Any probes still in flight are now stale (they share the HALF_OPEN
	// generation that just ended) and will be ignored on completion.
	b.probesInFlight = 0
	b.probeSuccessesRun = 0
	b.probeSeenFailure = false
}

func (b *Breaker) enterClosedLocked() {
	b.state = StateClosed
	b.generation++
	// Fresh window and probe accounting after recovery; older samples
	// describe the dead incident.
	b.filled = 0
	b.idx = 0
	b.probesInFlight = 0
	b.probeSuccessesRun = 0
	b.probeSeenFailure = false
	b.openedAt = time.Time{}
	b.openDeadline = time.Time{}
}

// Snapshot copies the current state into an immutable value.
func (b *Breaker) Snapshot() Snapshot {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.possiblyEnterHalfOpenLocked()

	var ws, wf, wc int
	for i := 0; i < b.filled; i++ {
		switch b.samples[i] {
		case OutcomeSuccess:
			ws++
		case OutcomeFailure:
			wf++
		case OutcomeCanceled:
			wc++
		}
	}

	free := 0
	if b.state == StateHalfOpen {
		free = b.cfg.MaxProbeCalls - b.probesInFlight
	}
	return Snapshot{
		State:            b.state,
		Generation:       b.generation,
		WindowTotal:      b.filled,
		WindowSuccesses:  ws,
		WindowFailures:   wf,
		WindowCanceled:   wc,
		InFlight:         b.inFlight,
		ProbePermitsFree: free,
		ProbeSuccesses:   b.probeSuccessesRun,
		OpenedAt:         b.openedAt,
		OpenDeadline:     b.openDeadline,
		TotalCalls:       b.totalCalls,
		TotalSuccesses:   b.totalSuccesses,
		TotalFailures:    b.totalFailures,
		TotalCanceled:    b.totalCanceled,
		TotalRejected:    b.totalRejected,
	}
}
