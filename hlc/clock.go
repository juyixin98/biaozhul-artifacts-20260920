// Package hlc implements a Hybrid Logical Clock (HLC) following the
// Kulkarni/Demirbas/Medda design:
//
//	type Timestamp = (physical milliseconds since epoch, logical counter)
//
// On a local event:
//
//	l' = (max(l, pt)) ; l = l' with the logical counter incremented.
//
// On receiving a remote timestamp m:
//
//	l' = max(l, m, pt); the counter is reset to 0 when the physical part
//	advances, otherwise it is one more than the larger of the local and
//	remote counters.
//
// The physical clock is injected (PhysicalFunc) so the algorithm and its
// tests never depend on wall-clock readings, and the physical clock can be
// made to jump backwards in tests.
package hlc

import (
	"errors"
	"fmt"
	"sync"
	"time"
)

// ErrOverflow is returned when the logical counter reaches its limit and the
// physical clock cannot be advanced past the current physical part (the wait
// hook refused, or too many advance rounds were attempted).
var ErrOverflow = errors.New("hlc: logical counter overflow")

// ErrInvalidTimestamp is returned when a remote timestamp fails validation
// (bad node id, negative physical part, logical counter above the configured
// limit, or malformed wire form).
var ErrInvalidTimestamp = errors.New("hlc: invalid timestamp")

// FutureDriftError is returned by Receive when the remote timestamp's
// physical part is more than MaxDrift ahead of the local physical clock.
// The message is rejected rather than absorbed, so a single far-future value
// cannot permanently poison the local clock.
type FutureDriftError struct {
	RemotePhysical int64
	LocalPhysical  int64
	MaxDrift       int64
}

func (e *FutureDriftError) Error() string {
	return fmt.Sprintf(
		"hlc: remote physical time %d is %dms ahead of local physical %d (limit %dms)",
		e.RemotePhysical, e.RemotePhysical-e.LocalPhysical, e.LocalPhysical, e.MaxDrift,
	)
}

// Is lets errors.Is(err, ErrFutureDrift) work through wrapping.
func (e *FutureDriftError) Is(target error) bool {
	_, ok := target.(*FutureDriftError)
	return ok
}

// ErrFutureDrift is the sentinel matched by errors.Is for FutureDriftError.
var ErrFutureDrift = &FutureDriftError{}

const (
	// DefaultMaxDrift is the default bound (milliseconds) on how far ahead of
	// the local physical clock a remote timestamp may be.
	DefaultMaxDrift int64 = 1000
	// DefaultMaxLogical is the default logical-counter limit (uint32 max).
	// Some deployments cap it lower (16 bits); see WithMaxLogical.
	DefaultMaxLogical uint64 = 1<<32 - 1
	// defaultMaxOverflowRounds bounds how many times an operation will sleep
	// waiting for physical time to advance when the counter is saturated.
	defaultMaxOverflowRounds = 1024
	// DefaultMaxOverflowWait bounds how long a single Tick/Receive may block
	// waiting for physical time to advance past a saturated counter. A remote
	// timestamp carrying a counter at the limit would otherwise make the call
	// sleep until an arbitrarily distant physical time.
	DefaultMaxOverflowWait = 250 * time.Millisecond
)

// PhysicalFunc returns the current physical time in Unix milliseconds.
type PhysicalFunc func() int64

// WaitFunc sleeps until the physical clock reports a value at least target,
// but no later than deadline. Reaching the deadline without the clock
// advancing returns ErrOverflow (the caller then surfaces overflow rather
// than blocking forever). Tests inject their own implementation.
type WaitFunc func(target int64, deadline time.Time) error

// Clock is a concurrency-safe hybrid logical clock.
type Clock struct {
	mu sync.Mutex

	nodeID          string
	physical        PhysicalFunc
	wait            WaitFunc
	maxDrift        int64
	maxLogical      uint64
	maxOverflowWait time.Duration

	pt int64  // physical part of the last issued timestamp
	lc uint64 // logical counter of the last issued timestamp

	overflowWaits uint64 // total overflows resolved (or attempted) by waiting
	tickCount     uint64
	receiveCount  uint64
	driftRejects  uint64
}

// Option configures a Clock.
type Option func(*Clock)

// WithPhysicalFunc injects the physical time source. Tests use a controllable
// fake; production uses the wall clock.
func WithPhysicalFunc(f PhysicalFunc) Option {
	return func(c *Clock) { c.physical = f }
}

// WithWaitFunc injects the hook used to wait for physical time to advance.
// Tests use it to simulate a clock that refuses to advance.
func WithWaitFunc(w WaitFunc) Option {
	return func(c *Clock) { c.wait = w }
}

// WithMaxDrift sets the accepted future drift in milliseconds.
func WithMaxDrift(d int64) Option {
	return func(c *Clock) { c.maxDrift = d }
}

// WithMaxLogical sets the logical counter limit.
func WithMaxLogical(max uint64) Option {
	return func(c *Clock) { c.maxLogical = max }
}

// WithMaxOverflowWait bounds how long one Tick/Receive may block while
// waiting for the physical clock to advance past a saturated counter.
func WithMaxOverflowWait(d time.Duration) Option {
	return func(c *Clock) { c.maxOverflowWait = d }
}

// New creates a Clock for nodeID.
func New(nodeID string, opts ...Option) (*Clock, error) {
	if err := ValidateNodeID(nodeID); err != nil {
		return nil, err
	}
	c := &Clock{
		nodeID:          nodeID,
		physical:        defaultPhysical,
		wait:            defaultWait,
		maxDrift:        DefaultMaxDrift,
		maxLogical:      DefaultMaxLogical,
		maxOverflowWait: DefaultMaxOverflowWait,
	}
	for _, o := range opts {
		o(c)
	}
	if c.physical == nil {
		return nil, errors.New("hlc: nil physical func")
	}
	if c.maxDrift < 0 {
		return nil, errors.New("hlc: max drift must be >= 0")
	}
	if c.maxLogical < 1 {
		return nil, errors.New("hlc: max logical must be >= 1")
	}
	if c.maxOverflowWait < 0 {
		return nil, errors.New("hlc: max overflow wait must be >= 0")
	}
	now := c.physical()
	if now < 0 {
		return nil, errors.New("hlc: initial physical time must be >= 0")
	}
	c.pt = now
	return c, nil
}

// NodeID returns the node identifier.
func (c *Clock) NodeID() string { return c.nodeID }

// MaxDrift returns the configured drift bound in milliseconds.
func (c *Clock) MaxDrift() int64 { return c.maxDrift }

// MaxLogical returns the configured logical counter limit.
func (c *Clock) MaxLogical() uint64 { return c.maxLogical }

// Tick issues the timestamp for a local event.
//
// If the wall clock moved backwards, or enough events happened within one
// physical millisecond to saturate the logical counter, Tick blocks (via the
// WaitFunc) until physical time advances past the current physical part —
// bounded by maxOverflowRounds — rather than emitting an invalid counter.
func (c *Clock) Tick() (Timestamp, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.tickCount++
	return c.tickLocked()
}

func (c *Clock) tickLocked() (Timestamp, error) {
	p := c.physical()
	deadline := c.deadline()
	rounds := 0
	for {
		if p > c.pt {
			c.pt = p
			c.lc = 0
			return Timestamp{Physical: c.pt, Logical: c.lc, NodeID: c.nodeID}, nil
		}
		if c.lc < c.maxLogical {
			c.lc++
			return Timestamp{Physical: c.pt, Logical: c.lc, NodeID: c.nodeID}, nil
		}
		// Counter saturated at this physical part: advance physical time.
		c.overflowWaits++
		if rounds >= defaultMaxOverflowRounds {
			return Timestamp{}, ErrOverflow
		}
		rounds++
		target := c.pt + 1
		if target <= c.pt { // int64 overflow; the year is ~292 million, be safe
			return Timestamp{}, ErrOverflow
		}
		if err := c.wait(target, deadline); err != nil {
			return Timestamp{}, err
		}
		p = c.physical()
	}
}

// deadline returns the absolute time by which overflow waiting must finish.
// A zero maxOverflowWait means the clock never waits (overflow is immediate),
// which is useful for tests and non-blocking deployments.
func (c *Clock) deadline() time.Time {
	if c.maxOverflowWait <= 0 {
		return time.Now() // already due
	}
	return time.Now().Add(c.maxOverflowWait)
}

// Receive absorbs a remote timestamp (message-receive / send event) and
// returns the timestamp to stamp the receive-side event with.
//
// The remote value is rejected when its physical part is more than MaxDrift
// beyond the local physical clock; stale or equal values are merged normally,
// which is also what makes wall-clock backwards jumps transparent.
func (c *Clock) Receive(remote Timestamp) (Timestamp, error) {
	if err := remote.Validate(); err != nil {
		return Timestamp{}, err
	}
	if remote.Logical > c.maxLogical {
		return Timestamp{}, fmt.Errorf("%w: logical %d exceeds limit %d",
			ErrInvalidTimestamp, remote.Logical, c.maxLogical)
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.receiveCount++

	p := c.physical()
	if d := remote.Physical - p; d > c.maxDrift {
		c.driftRejects++
		return Timestamp{}, &FutureDriftError{
			RemotePhysical: remote.Physical,
			LocalPhysical:  p,
			MaxDrift:       c.maxDrift,
		}
	}

	deadline := c.deadline()
	rounds := 0
	for {
		p = c.physical()
		newPT, newLC, overflow := merge(c.pt, c.lc, remote, p, c.maxLogical)
		if !overflow {
			c.pt, c.lc = newPT, newLC
			return Timestamp{Physical: c.pt, Logical: c.lc, NodeID: c.nodeID}, nil
		}
		// Resulting counter would exceed the limit (typically a remote
		// timestamp carrying a counter at the limit): wait until physical
		// time advances past newPT, then re-merge. Once p > newPT the merge
		// lands on the fresh-physical branch (counter 0), so it terminates.
		c.overflowWaits++
		if rounds >= defaultMaxOverflowRounds {
			return Timestamp{}, ErrOverflow
		}
		rounds++
		target := newPT + 1
		if target <= newPT {
			return Timestamp{}, ErrOverflow
		}
		if err := c.wait(target, deadline); err != nil {
			return Timestamp{}, err
		}
	}
}

// merge computes l' = max(local, remote, physicalNow) per the HLC paper and
// returns the (physical part, logical counter) of the receive event:
//
//   - physical time strictly dominates both stored values -> counter resets 0
//   - the max physical part ties with a stored value -> larger counter + 1
//   - local physical part is the unique max -> local counter + 1
//   - remote physical part is the unique max -> remote counter + 1
//
// overflow is true when the required counter would exceed maxLogical (this
// includes the uint64 wrap case, which a plain "+1 <= max" check would miss);
// the caller waits for physical time to advance past the returned physical
// part and re-merges.
func merge(localPT int64, localLC uint64, remote Timestamp, p int64, maxLogical uint64) (newPT int64, newLC uint64, overflow bool) {
	bump := func(base uint64) (uint64, bool) {
		if base >= maxLogical {
			return 0, true
		}
		return base + 1, false
	}
	switch {
	case p >= localPT && p >= remote.Physical:
		if p == localPT || p == remote.Physical {
			lc := uint64(0)
			if p == localPT && localLC > lc {
				lc = localLC
			}
			if p == remote.Physical && remote.Logical > lc {
				lc = remote.Logical
			}
			out, ov := bump(lc)
			return p, out, ov
		}
		return p, 0, false
	case localPT >= remote.Physical: // local is the max; p is not the max here
		if localPT == remote.Physical {
			lc := localLC
			if remote.Logical > lc {
				lc = remote.Logical
			}
			out, ov := bump(lc)
			return localPT, out, ov
		}
		out, ov := bump(localLC)
		return localPT, out, ov
	default: // remote physical part is the strict max
		out, ov := bump(remote.Logical)
		return remote.Physical, out, ov
	}
}

// Now returns the timestamp the clock would stamp a new local event with,
// updating the clock state (same semantics as Tick but named for the
// read-current-time endpoint).
func (c *Clock) Now() (Timestamp, error) {
	return c.Tick()
}

// Peek returns the clock's current state without advancing it and without
// consulting the physical clock — useful for status/health reporting.
func (c *Clock) Peek() Timestamp {
	c.mu.Lock()
	defer c.mu.Unlock()
	return Timestamp{Physical: c.pt, Logical: c.lc, NodeID: c.nodeID}
}

// Stats is a point-in-time snapshot of clock counters.
type Stats struct {
	Last           Timestamp `json:"last"`
	PhysicalNow    int64     `json:"physical_now_ms"`
	MaxDrift       int64     `json:"max_drift_ms"`
	MaxLogical     uint64    `json:"max_logical"`
	OverflowWaits  uint64    `json:"overflow_waits"`
	TickCount      uint64    `json:"tick_count"`
	ReceiveCount   uint64    `json:"receive_count"`
	DriftRejects   uint64    `json:"drift_rejects"`
	BehindPhysical bool      `json:"behind_physical"` // last physical part < wall clock (healthy)
	SkewMs         int64     `json:"skew_ms"`         // last physical part - wall clock
}

// Stats returns a snapshot of the clock's internal counters.
func (c *Clock) Stats() Stats {
	c.mu.Lock()
	defer c.mu.Unlock()
	p := c.physical()
	last := Timestamp{Physical: c.pt, Logical: c.lc, NodeID: c.nodeID}
	return Stats{
		Last:           last,
		PhysicalNow:    p,
		MaxDrift:       c.maxDrift,
		MaxLogical:     c.maxLogical,
		OverflowWaits:  c.overflowWaits,
		TickCount:      c.tickCount,
		ReceiveCount:   c.receiveCount,
		DriftRejects:   c.driftRejects,
		BehindPhysical: c.pt < p,
		SkewMs:         c.pt - p,
	}
}

func defaultPhysical() int64 {
	return time.Now().UnixMilli()
}

// defaultWait sleeps until the wall clock reaches target, giving up at
// deadline. Overflow waiting is only meant to bridge a single physical
// millisecond during a same-ms burst; a target beyond the deadline means the
// clock cannot catch up in budget (e.g. a remote timestamp whose counter is
// pinned at the limit), so it returns ErrOverflow instead of blocking.
func defaultWait(target int64, deadline time.Time) error {
	for {
		if time.Now().After(deadline) {
			return ErrOverflow
		}
		remaining := time.UnixMilli(target).Sub(time.Now())
		if remaining <= 0 {
			return nil
		}
		if budget := time.Until(deadline); budget < remaining {
			remaining = budget
		}
		if remaining > time.Millisecond {
			remaining = time.Millisecond
		}
		time.Sleep(remaining)
	}
}
