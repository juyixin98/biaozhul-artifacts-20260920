// Package clock defines the replaceable time source used by the scheduler.
//
// The simulation itself is a discrete-event model that advances time in fixed
// integer ticks, but Clock is the abstraction executors and other components
// see. Two implementations are provided:
//
//   - VirtualClock: fully deterministic, manually advanced; used by the
//     simulator and by unit tests.
//   - WallClock: reads the operating-system clock; useful when plugging in an
//     executor that needs real time (see package examples).
package clock

import (
	"sync"
	"sync/atomic"
	"time"
)

// Clock is the minimal time source contract. Ticks are the scheduler's unit
// of logical time; WallTime maps logical time onto a physical timeline for
// executors that want to sleep or timestamp external output.
type Clock interface {
	// Now returns the current logical time in ticks.
	Now() int64
	// Advance moves the clock forward by d ticks. Implementations may panic
	// on d < 0.
	Advance(d int64)
	// WallTime returns the physical time associated with the current logical
	// instant.
	WallTime() time.Time
}

// VirtualClock is a manually driven, concurrency-safe clock. Time stands
// still until Advance is called, which makes simulations reproducible.
type VirtualClock struct {
	ticks atomic.Int64
}

// NewVirtualClock returns a virtual clock starting at tick 0 (or at start).
func NewVirtualClock(start int64) *VirtualClock {
	c := &VirtualClock{}
	c.ticks.Store(start)
	return c
}

func (c *VirtualClock) Now() int64 { return c.ticks.Load() }

func (c *VirtualClock) Advance(d int64) {
	if d < 0 {
		panic("clock: cannot advance virtual clock backwards")
	}
	c.ticks.Add(d)
}

// WallTime for a virtual clock has no meaningful physical mapping, so the
// Unix epoch plus the tick count is returned deterministically.
func (c *VirtualClock) WallTime() time.Time {
	return time.UnixMilli(c.ticks.Load()).UTC()
}

// WallClock reads real time. Tick still exists in the interface, so callers
// must provide the current logical tick separately (the simulator keeps its
// own counter); Advance is a no-op.
type WallClock struct {
	mu   sync.Mutex
	base time.Time
	tick int64
}

// NewWallClock returns a wall clock whose WallTime is time.Now().
func NewWallClock() *WallClock {
	return &WallClock{base: time.Now()}
}

func (c *WallClock) Now() int64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.tick
}

func (c *WallClock) Advance(d int64) {
	if d < 0 {
		panic("clock: negative advance")
	}
	c.mu.Lock()
	c.tick += d
	c.mu.Unlock()
}

func (c *WallClock) WallTime() time.Time { return time.Now().UTC() }
