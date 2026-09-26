// Package breaker implements a circuit breaker with a sliding call-sample
// window, bounded half-open probing and generation-tagged permits.
//
// The concurrency guarantees this package is built around:
//
//   - Half-open probe slots are limited (HalfOpenMaxProbes). Further calls
//     while the slots are occupied fail fast instead of flooding a possibly
//     still-broken dependency.
//   - Every permit is stamped with the breaker generation it was issued in.
//     A result arriving after the breaker has moved to a newer generation
//     (open -> half-open -> open, half-open -> closed, ...) is stale: it is
//     accounted for observability but can never mutate the new generation's
//     state or counters.
//   - A canceled call (context canceled by the caller) only releases the
//     permit. It is recorded as "canceled", never as a failure, and therefore
//     can neither trip the breaker nor fail a half-open probe.
package breaker

import (
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"time"

	"cbhalfopen/internal/vclock"
)

// State of the breaker.
type State string

// Supported states.
const (
	StateClosed   State = "closed"
	StateOpen     State = "open"
	StateHalfOpen State = "half_open"
)

// Outcome of a permitted call.
type Outcome string

// Supported outcomes.
const (
	OutcomeSuccess  Outcome = "success"
	OutcomeFailure  Outcome = "failure"
	OutcomeCanceled Outcome = "canceled"
)

var (
	// ErrOpen is returned while the breaker is open and the cooldown has
	// not elapsed.
	ErrOpen = errors.New("circuit breaker is open")
	// ErrProbesExhausted is returned in half-open state when every probe
	// slot is occupied by an in-flight probe.
	ErrProbesExhausted = errors.New("half-open probe slots exhausted")
)

// Config configures a Breaker.
type Config struct {
	// SlidingWindowSize is the maximum number of the most recent terminal
	// calls kept as samples.
	SlidingWindowSize int
	// MinRequests is the minimum number of completed (non-canceled)
	// samples required before the failure ratio may trip the breaker.
	MinRequests int
	// FailureThreshold is the failure ratio in [0,1] at or above which the
	// breaker opens while in closed state.
	FailureThreshold float64
	// OpenCooldown is how long the breaker stays open before a transition
	// to half-open is allowed.
	OpenCooldown time.Duration
	// HalfOpenMaxProbes is the number of probe calls allowed to be
	// in flight at the same time during one half-open generation.
	HalfOpenMaxProbes int
	// RequiredSuccesses is the number of consecutive successful probes
	// that closes the breaker.
	RequiredSuccesses int
	// Clock is the time source. Defaults to the wall clock.
	Clock vclock.Clock
	// OnTransition, when set, is invoked after every state transition
	// (never while holding the breaker lock).
	OnTransition func(Transition)
}

func (c *Config) applyDefaults() {
	if c.SlidingWindowSize == 0 {
		c.SlidingWindowSize = 20
	}
	if c.MinRequests == 0 {
		c.MinRequests = 10
	}
	if c.MinRequests > c.SlidingWindowSize {
		c.MinRequests = c.SlidingWindowSize
	}
	if c.FailureThreshold == 0 {
		c.FailureThreshold = 0.5
	}
	if c.OpenCooldown == 0 {
		c.OpenCooldown = 5 * time.Second
	}
	if c.HalfOpenMaxProbes == 0 {
		c.HalfOpenMaxProbes = 3
	}
	if c.RequiredSuccesses == 0 {
		c.RequiredSuccesses = 2
	}
	if c.Clock == nil {
		c.Clock = vclock.SystemClock{}
	}
}

func (c Config) validate() error {
	if c.SlidingWindowSize <= 0 {
		return errors.New("SlidingWindowSize must be positive")
	}
	if c.MinRequests <= 0 {
		return errors.New("MinRequests must be positive")
	}
	if c.FailureThreshold <= 0 || c.FailureThreshold > 1 {
		return fmt.Errorf("FailureThreshold must be in (0,1], got %v", c.FailureThreshold)
	}
	if c.OpenCooldown <= 0 {
		return errors.New("OpenCooldown must be positive")
	}
	if c.HalfOpenMaxProbes <= 0 {
		return errors.New("HalfOpenMaxProbes must be positive")
	}
	if c.RequiredSuccesses <= 0 {
		return errors.New("RequiredSuccesses must be positive")
	}
	return nil
}

// Counters are lifetime counters for the current breaker instance.
type Counters struct {
	Allowed        int64 `json:"allowed"`
	Rejected       int64 `json:"rejected"`
	Successes      int64 `json:"successes"`
	Failures       int64 `json:"failures"`
	Canceled       int64 `json:"canceled"`
	ProbesGranted  int64 `json:"probes_granted"`
	ProbesRejected int64 `json:"probes_rejected"`
	// StaleResults is the number of permit completions belonging to an old
	// generation that were discarded because the breaker had moved on.
	StaleResults int64 `json:"stale_results"`
}

// Transition describes one breaker state change.
type Transition struct {
	Time       time.Time `json:"time"`
	From       State     `json:"from"`
	To         State     `json:"to"`
	Generation uint64    `json:"generation"`
	Reason     string    `json:"reason"`
}

// Sample is one sliding-window entry.
type Sample struct {
	Time       time.Time `json:"time"`
	Generation uint64    `json:"generation"`
	Outcome    Outcome   `json:"outcome"`
}

// Snapshot is an immutable point-in-time view of the breaker.
type Snapshot struct {
	State                State      `json:"state"`
	Generation           uint64     `json:"generation"`
	Now                  time.Time  `json:"now"`
	CooldownUntil        time.Time  `json:"cooldown_until,omitempty"`
	ProbesInFlight       int        `json:"probes_in_flight"`
	HalfOpenMaxProbes    int        `json:"half_open_max_probes"`
	ConsecutiveSuccesses int        `json:"consecutive_successes"`
	RequiredSuccesses    int        `json:"required_successes"`
	WindowSamples        int        `json:"window_samples"`
	WindowSuccesses      int        `json:"window_successes"`
	WindowFailures       int        `json:"window_failures"`
	WindowCanceled       int        `json:"window_canceled"`
	FailureRatio         float64    `json:"failure_ratio"`
	Counters             Counters   `json:"counters"`
	Config               ConfigView `json:"config"`
}

// ConfigView is the JSON-safe representation of the effective configuration.
type ConfigView struct {
	SlidingWindowSize int           `json:"sliding_window_size"`
	MinRequests       int           `json:"min_requests"`
	FailureThreshold  float64       `json:"failure_threshold"`
	OpenCooldown      time.Duration `json:"open_cooldown"`
	HalfOpenMaxProbes int           `json:"half_open_max_probes"`
	RequiredSuccesses int           `json:"required_successes"`
}

type sample struct {
	time       time.Time
	generation uint64
	outcome    Outcome
}

// Breaker is the circuit breaker. The zero value is not usable; use New.
type Breaker struct {
	cfg Config

	mu              sync.Mutex
	state           State
	generation      uint64
	cooldownUntil   time.Time
	ring            []sample
	probesInFlight  int
	consecSuccesses int
	ctr             Counters
	transitions     []Transition
}

// New constructs a breaker. Zero-valued Config fields receive safe defaults.
func New(cfg Config) (*Breaker, error) {
	cfg.applyDefaults()
	if err := cfg.validate(); err != nil {
		return nil, err
	}
	b := &Breaker{
		cfg:   cfg,
		state: StateClosed,
		ring:  make([]sample, 0, cfg.SlidingWindowSize),
	}
	b.pushTransitionLocked(StateClosed, StateClosed, 0, "initialized")
	return b, nil
}

// Permit grants one call through the breaker. Exactly one of RecordSuccess,
// RecordFailure or RecordCanceled must be called once the call finishes.
type Permit struct {
	b          *Breaker
	generation uint64
	probe      bool
	finished   atomic.Bool
}

// Generation reports the breaker generation this permit belongs to.
func (p *Permit) Generation() uint64 { return p.generation }

// IsProbe reports whether the permit is a half-open probe.
func (p *Permit) IsProbe() bool { return p.probe }

// RecordSuccess completes the permit with a successful result.
func (p *Permit) RecordSuccess() { p.finish(OutcomeSuccess) }

// RecordFailure completes the permit with a service failure (error response,
// timeout, transport error). A caller-side cancellation must use
// RecordCanceled instead.
func (p *Permit) RecordFailure() { p.finish(OutcomeFailure) }

// RecordCanceled completes the permit after the caller gave up (context
// cancellation). It releases any probe slot but is never counted as a service
// failure and never drives a state transition.
func (p *Permit) RecordCanceled() { p.finish(OutcomeCanceled) }

func (p *Permit) finish(o Outcome) {
	b := p.b
	b.mu.Lock()

	if p.finished.Swap(true) {
		// Defensive: a permit must only be completed once.
		b.mu.Unlock()
		return
	}

	stale := p.generation != b.generation
	if stale {
		b.ctr.StaleResults++
		b.mu.Unlock()
		return
	}

	now := b.cfg.Clock.Now()

	switch o {
	case OutcomeSuccess:
		b.ctr.Successes++
	case OutcomeFailure:
		b.ctr.Failures++
	case OutcomeCanceled:
		b.ctr.Canceled++
	}

	if p.probe {
		// Probe slots are reset on every generation change, and a
		// same-generation permit always owns one slot here.
		b.probesInFlight--
	}

	var transitions []Transition
	switch b.state {
	case StateClosed:
		if o != OutcomeCanceled {
			b.ring = append(b.ring, sample{time: now, generation: b.generation, outcome: o})
			if len(b.ring) > b.cfg.SlidingWindowSize {
				b.ring = b.ring[len(b.ring)-b.cfg.SlidingWindowSize:]
			}
			// Any completed sample can push the sliding window over the
			// ratio, not only the newest failure.
			if b.shouldTripLocked() {
				transitions = b.openLocked(now, "failure_threshold_exceeded")
			}
		}
	case StateHalfOpen:
		switch o {
		case OutcomeSuccess:
			b.consecSuccesses++
			if b.consecSuccesses >= b.cfg.RequiredSuccesses {
				transitions = b.closeLocked(now)
			}
		case OutcomeFailure:
			transitions = b.openLocked(now, "probe_failure")
		case OutcomeCanceled:
			// Release only; a caller cancellation is not a service
			// failure and does not reset the success streak.
		}
	}

	b.mu.Unlock()

	for _, t := range transitions {
		if b.cfg.OnTransition != nil {
			b.cfg.OnTransition(t)
		}
	}
}

// Allow checks whether a call may proceed. In closed state it always grants a
// permit. In open state it either rejects or flips the breaker to half-open
// once the cooldown has elapsed. In half-open it grants a bounded probe
// permit or rejects when the slots are full.
func (b *Breaker) Allow() (*Permit, error) {
	b.mu.Lock()
	now := b.cfg.Clock.Now()
	var transition *Transition

	switch b.state {
	case StateClosed:
		b.ctr.Allowed++
		p := &Permit{b: b, generation: b.generation}
		b.mu.Unlock()
		return p, nil

	case StateOpen:
		if now.Before(b.cooldownUntil) {
			b.ctr.Rejected++
			b.mu.Unlock()
			return nil, ErrOpen
		}
		b.enterHalfOpenLocked(now)
		b.ctr.Allowed++
		b.ctr.ProbesGranted++
		b.probesInFlight = 1
		p := &Permit{b: b, generation: b.generation, probe: true}
		t := b.lastTransition()
		transition = &t
		b.mu.Unlock()
		b.fireTransition(transition)
		return p, nil

	case StateHalfOpen:
		if b.probesInFlight >= b.cfg.HalfOpenMaxProbes {
			b.ctr.Rejected++
			b.ctr.ProbesRejected++
			b.mu.Unlock()
			return nil, ErrProbesExhausted
		}
		b.ctr.Allowed++
		b.ctr.ProbesGranted++
		b.probesInFlight++
		p := &Permit{b: b, generation: b.generation, probe: true}
		b.mu.Unlock()
		return p, nil

	default:
		b.mu.Unlock()
		panic(fmt.Sprintf("breaker: unknown state %q", b.state))
	}
}

func (b *Breaker) fireTransition(t *Transition) {
	if b.cfg.OnTransition != nil && t != nil {
		b.cfg.OnTransition(*t)
	}
}

func (b *Breaker) shouldTripLocked() bool {
	failures, completed := 0, 0
	for _, s := range b.ring {
		switch s.outcome {
		case OutcomeFailure:
			failures++
			completed++
		case OutcomeSuccess:
			completed++
		}
	}
	if completed < b.cfg.MinRequests {
		return false
	}
	return float64(failures)/float64(completed) >= b.cfg.FailureThreshold
}

// enterHalfOpenLocked starts a new half-open generation. The first probe slot
// is accounted by the caller (Allow).
func (b *Breaker) enterHalfOpenLocked(now time.Time) {
	from := b.state
	b.state = StateHalfOpen
	b.generation++
	b.ring = b.ring[:0]
	b.probesInFlight = 0
	b.consecSuccesses = 0
	b.cooldownUntil = time.Time{}
	b.pushTransitionLocked(from, StateHalfOpen, b.generation, "cooldown_elapsed")
}

func (b *Breaker) openLocked(now time.Time, reason string) []Transition {
	from := b.state
	b.state = StateOpen
	b.generation++
	b.ring = b.ring[:0]
	b.probesInFlight = 0
	b.consecSuccesses = 0
	b.cooldownUntil = now.Add(b.cfg.OpenCooldown)
	b.pushTransitionLocked(from, StateOpen, b.generation, reason)
	return []Transition{b.lastTransition()}
}

func (b *Breaker) closeLocked(now time.Time) []Transition {
	from := b.state
	b.state = StateClosed
	b.generation++
	b.ring = b.ring[:0]
	b.probesInFlight = 0
	b.consecSuccesses = 0
	b.cooldownUntil = time.Time{}
	b.pushTransitionLocked(from, StateClosed, b.generation, "probes_succeeded")
	return []Transition{b.lastTransition()}
}

func (b *Breaker) pushTransitionLocked(from, to State, gen uint64, reason string) {
	t := Transition{
		Time:       b.cfg.Clock.Now(),
		From:       from,
		To:         to,
		Generation: gen,
		Reason:     reason,
	}
	b.transitions = append(b.transitions, t)
	if len(b.transitions) > 64 {
		b.transitions = b.transitions[len(b.transitions)-64:]
	}
}

func (b *Breaker) lastTransition() Transition {
	return b.transitions[len(b.transitions)-1]
}

// Snapshot returns a point-in-time view of breaker state and counters.
func (b *Breaker) Snapshot() Snapshot {
	b.mu.Lock()
	defer b.mu.Unlock()

	var s, f, c int
	for _, sample := range b.ring {
		switch sample.outcome {
		case OutcomeSuccess:
			s++
		case OutcomeFailure:
			f++
		case OutcomeCanceled:
			c++
		}
	}
	completed := s + f
	ratio := 0.0
	if completed > 0 {
		ratio = float64(f) / float64(completed)
	}

	return Snapshot{
		State:                b.state,
		Generation:           b.generation,
		Now:                  b.cfg.Clock.Now(),
		CooldownUntil:        b.cooldownUntil,
		ProbesInFlight:       b.probesInFlight,
		HalfOpenMaxProbes:    b.cfg.HalfOpenMaxProbes,
		ConsecutiveSuccesses: b.consecSuccesses,
		RequiredSuccesses:    b.cfg.RequiredSuccesses,
		WindowSamples:        len(b.ring),
		WindowSuccesses:      s,
		WindowFailures:       f,
		WindowCanceled:       c,
		FailureRatio:         ratio,
		Counters:             b.ctr,
		Config: ConfigView{
			SlidingWindowSize: b.cfg.SlidingWindowSize,
			MinRequests:       b.cfg.MinRequests,
			FailureThreshold:  b.cfg.FailureThreshold,
			OpenCooldown:      b.cfg.OpenCooldown,
			HalfOpenMaxProbes: b.cfg.HalfOpenMaxProbes,
			RequiredSuccesses: b.cfg.RequiredSuccesses,
		},
	}
}

// Transitions returns a copy of the recorded transition history.
func (b *Breaker) Transitions() []Transition {
	b.mu.Lock()
	defer b.mu.Unlock()
	out := make([]Transition, len(b.transitions))
	copy(out, b.transitions)
	return out
}
