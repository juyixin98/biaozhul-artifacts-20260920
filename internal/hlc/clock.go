package hlc

import (
	"errors"
	"fmt"
	"time"
)

var (
	// ErrOverflow is returned when the logical counter reaches its limit and
	// the physical clock cannot be advanced past the current physical part
	// within the overflow-wait budget.
	ErrOverflow = errors.New("hlc: logical counter overflow")
)

// Default tuning constants.
const (
	// DefaultMaxDrift is the default bound (milliseconds) on how far ahead of
	// the local physical clock an accepted remote timestamp may be.
	DefaultMaxDrift int64 = 1000
	// defaultMaxOverflowRounds caps the number of physical-advance rounds in
	// a single Tick/Receive so a pathological clock cannot loop forever.
	defaultMaxOverflowRounds = 1024
	// DefaultMaxOverflowWait bounds how long one Tick/Receive may block while
	// waiting for physical time to advance past a saturated counter.
	DefaultMaxOverflowWait = 250 * time.Millisecond
)

// FutureDriftError is returned by Receive when the remote timestamp's
// physical part is more than MaxDrift ahead of the local physical clock.
type FutureDriftError struct {
	RemotePhysical int64
	LocalPhysical  int64
	MaxDrift       int64
}

func (e *FutureDriftError) Error() string {
	return fmt.Sprintf(
		"hlc: remote physical time %d is %dms ahead of local physical %d (limit %dms)",
		e.RemotePhysical, e.RemotePhysical-e.LocalPhysical, e.LocalPhysical, e.MaxDrift)
}

// Is treats every *FutureDriftError as equal to the package sentinel, so
// errors.Is(err, ErrFutureDrift) works through wrapping.
func (e *FutureDriftError) Is(target error) bool {
	_, ok := target.(*FutureDriftError)
	return ok
}

// ErrFutureDrift is the sentinel matched by errors.Is for FutureDriftError.
var ErrFutureDrift = &FutureDriftError{}

// PhysicalClock abstracts the physical time source. Production uses the wall
// clock; tests inject a controllable fake. All correctness assertions are
// made against injected values — they never depend on the real wall clock.
type PhysicalClock interface {
	Now() time.Time
}

type systemClock struct{}

func (systemClock) Now() time.Time { return time.Now() }

// Config configures a Clock.
type Config struct {
	// NodeID stamps every timestamp this node issues. Required.
	NodeID string

	// MaxDriftMS is the accepted future-drift budget in milliseconds.
	// Zero/negative values use DefaultMaxDrift.
	MaxDriftMS int64

	// MaxLogical is the logical-counter ceiling. Defaults to 2^32-1.
	MaxLogical uint64

	// OverflowWaitTimeout bounds a single Tick/Receive that is waiting for
	// physical time to pass a saturated counter. Zero uses the default;
	// a negative value means "never wait" (overflow is immediate).
	OverflowWaitTimeout time.Duration

	// Physical injects the physical clock. nil uses the system wall clock.
	Physical PhysicalClock

	// PollInterval controls how often the overflow wait re-reads the physical
	// clock. Production default 1ms; tests set a tiny value.
	PollInterval time.Duration
}

// Clock is a thread-safe hybrid logical clock.
type Clock struct {
	// mu is a 1-capacity channel used as a mutex (makes held state explicit).
	mu chan struct{}

	nodeID       string
	physical     PhysicalClock
	maxDrift     int64
	maxLogical   uint64
	overflowWait time.Duration
	poll         time.Duration

	pt int64  // physical part of the last issued timestamp
	lc uint64 // logical counter of the last issued timestamp

	overflowWaits int64
	tickCount     int64
	receiveCount  int64
	driftRejects  int64
}

// NewClock constructs a Clock from a Config.
func NewClock(cfg Config) (*Clock, error) {
	if err := ValidateNodeID(cfg.NodeID); err != nil {
		return nil, err
	}
	physical := cfg.Physical
	if physical == nil {
		physical = systemClock{}
	}
	maxDrift := cfg.MaxDriftMS
	if maxDrift == 0 {
		maxDrift = DefaultMaxDrift
	}
	if maxDrift < 0 {
		return nil, errors.New("hlc: max drift must be >= 0")
	}
	maxLogical := cfg.MaxLogical
	if maxLogical == 0 {
		maxLogical = DefaultMaxLogical
	}
	wait := cfg.OverflowWaitTimeout
	if wait == 0 {
		wait = DefaultMaxOverflowWait
	}
	poll := cfg.PollInterval
	if poll <= 0 {
		poll = time.Millisecond
	}
	now := physical.Now().UnixMilli()
	if now < 0 {
		return nil, errors.New("hlc: initial physical time must be >= 0")
	}
	return &Clock{
		mu:           make(chan struct{}, 1),
		nodeID:       cfg.NodeID,
		physical:     physical,
		maxDrift:     maxDrift,
		maxLogical:   maxLogical,
		overflowWait: wait,
		poll:         poll,
		pt:           now,
	}, nil
}

// NodeID returns the clock's node identifier.
func (c *Clock) NodeID() string { return c.nodeID }

// MaxDrift returns the configured future-drift budget in milliseconds.
func (c *Clock) MaxDrift() int64 { return c.maxDrift }

// MaxLogical returns the configured logical-counter ceiling.
func (c *Clock) MaxLogical() uint64 { return c.maxLogical }

func (c *Clock) lock()   { c.mu <- struct{}{} }
func (c *Clock) unlock() { <-c.mu }

// Snapshot returns the last issued timestamp without advancing the clock.
func (c *Clock) Snapshot() Timestamp {
	c.lock()
	defer c.unlock()
	return Timestamp{Physical: c.pt, Logical: c.lc, NodeID: c.nodeID}
}

// Tick issues the timestamp for a local event.
func (c *Clock) Tick() (Timestamp, error) {
	c.lock()
	defer c.unlock()
	c.tickCount++

	p := c.physical.Now().UnixMilli()
	deadline := c.deadline()
	rounds := 0
	for {
		if p > c.pt {
			c.pt, c.lc = p, 0
			return Timestamp{Physical: c.pt, Logical: c.lc, NodeID: c.nodeID}, nil
		}
		if c.lc < c.maxLogical {
			c.lc++
			return Timestamp{Physical: c.pt, Logical: c.lc, NodeID: c.nodeID}, nil
		}
		// Counter saturated at this physical part: wait for physical time.
		c.overflowWaits++
		if rounds >= defaultMaxOverflowRounds {
			return Timestamp{}, ErrOverflow
		}
		rounds++
		target := c.pt + 1
		if target <= c.pt { // int64 overflow guard
			return Timestamp{}, ErrOverflow
		}
		if err := c.waitForWall(target, deadline); err != nil {
			return Timestamp{}, err
		}
		p = c.physical.Now().UnixMilli()
	}
}

// Receive merges a remote timestamp and returns the receive-event timestamp.
// A remote value more than MaxDrift ahead of the local physical clock is
// rejected (and local state is untouched). Backwards or equal values merge
// normally, which is what makes physical clock rollback transparent.
func (c *Clock) Receive(remote Timestamp) (Timestamp, error) {
	if err := remote.Validate(); err != nil {
		return Timestamp{}, err
	}
	if remote.Logical > c.maxLogical {
		return Timestamp{}, fmt.Errorf("%w: logical %d exceeds limit %d",
			ErrInvalidTimestamp, remote.Logical, c.maxLogical)
	}

	c.lock()
	defer c.unlock()
	c.receiveCount++

	p := c.physical.Now().UnixMilli()
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
		newPT, newLC, overflow := merge(c.pt, c.lc, remote, p, c.maxLogical)
		if !overflow {
			c.pt, c.lc = newPT, newLC
			return Timestamp{Physical: c.pt, Logical: c.lc, NodeID: c.nodeID}, nil
		}
		c.overflowWaits++
		if rounds >= defaultMaxOverflowRounds {
			return Timestamp{}, ErrOverflow
		}
		rounds++
		target := newPT + 1
		if target <= newPT {
			return Timestamp{}, ErrOverflow
		}
		if err := c.waitForWall(target, deadline); err != nil {
			return Timestamp{}, err
		}
		p = c.physical.Now().UnixMilli()
	}
}

// deadline is the absolute time by which overflow waiting must finish. A
// non-positive overflow wait means "already due", i.e. overflow is immediate.
func (c *Clock) deadline() time.Time {
	if c.overflowWait <= 0 {
		return time.Now()
	}
	return time.Now().Add(c.overflowWait)
}

// waitForWall blocks until the injected physical clock reads >= target, or
// until deadline. Called with the clock lock held.
func (c *Clock) waitForWall(target int64, deadline time.Time) error {
	for {
		if time.Now().After(deadline) {
			return ErrOverflow
		}
		if c.physical.Now().UnixMilli() >= target {
			return nil
		}
		remaining := time.Until(deadline)
		if remaining <= 0 {
			return ErrOverflow
		}
		step := c.poll
		if remaining < step {
			step = remaining
		}
		time.Sleep(step)
	}
}

// merge computes l' = max(local, remote, physicalNow) and returns the
// (physical, logical) pair for the event, plus overflow=true when the logical
// counter required would exceed maxLogical.
func merge(localPT int64, localLC uint64, remote Timestamp, p int64, maxLogical uint64) (newPT int64, newLC uint64, overflow bool) {
	bump := func(base uint64) (uint64, bool) {
		if base >= maxLogical {
			return 0, true
		}
		return base + 1, false
	}
	switch {
	// Physical now is a (non-strict) maximum: if it equals either stored
	// physical part, that stored logical counter feeds the max+1 rule.
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
	case localPT >= remote.Physical:
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
	default:
		out, ov := bump(remote.Logical)
		return remote.Physical, out, ov
	}
}

// Stats is a point-in-time operational snapshot.
type Stats struct {
	Last           Timestamp `json:"last"`
	PhysicalNow    int64     `json:"physical_now_ms"`
	MaxDrift       int64     `json:"max_drift_ms"`
	MaxLogical     uint64    `json:"max_logical"`
	OverflowWaits  int64     `json:"overflow_waits"`
	TickCount      int64     `json:"tick_count"`
	ReceiveCount   int64     `json:"receive_count"`
	DriftRejects   int64     `json:"drift_rejects"`
	BehindPhysical bool      `json:"behind_physical"`
	SkewMs         int64     `json:"skew_ms"`
}

// Stats returns a point-in-time operational snapshot. Safe for concurrent use.
func (c *Clock) Stats() Stats {
	c.lock()
	defer c.unlock()
	p := c.physical.Now().UnixMilli()
	return Stats{
		Last:           Timestamp{Physical: c.pt, Logical: c.lc, NodeID: c.nodeID},
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
