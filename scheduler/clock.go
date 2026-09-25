package scheduler

import "time"

// Clock abstracts time so the simulation kernel is decoupled from any wall
// clock. The default simulation uses SimClock (deterministic integer ticks);
// tests and alternative front-ends can supply their own implementation.
type Clock interface {
	// Now returns the current logical time.
	Now() int64
	// Advance moves the clock forward by d (d >= 0).
	Advance(d int64)
}

// SimClock is the default deterministic clock: a plain tick counter.
type SimClock struct{ t int64 }

func NewSimClock() *SimClock   { return &SimClock{} }
func (c *SimClock) Now() int64 { return c.t }
func (c *SimClock) Advance(d int64) {
	if d > 0 {
		c.t += d
	}
}

// CountingClock wraps another Clock and counts total advancement.
type CountingClock struct {
	Inner Clock
	Steps int64
}

func NewCountingClock(inner Clock) *CountingClock {
	if inner == nil {
		inner = NewSimClock()
	}
	return &CountingClock{Inner: inner}
}
func (c *CountingClock) Now() int64      { return c.Inner.Now() }
func (c *CountingClock) Advance(d int64) { c.Steps += d; c.Inner.Advance(d) }

// WallClock maps logical ticks onto real elapsed time at a fixed tick
// duration, demonstrating a replaceable clock that ties to wall time.
// Now() is driven by Advance(); the wall offset is set at construction.
type WallClock struct {
	start time.Time
	dur   time.Duration
	t     int64
}

// NewWallClock creates a clock where one tick takes tickDuration of wall time,
// anchored at start (time.Now if zero).
func NewWallClock(tickDuration time.Duration, start time.Time) *WallClock {
	if tickDuration <= 0 {
		tickDuration = time.Millisecond
	}
	if start.IsZero() {
		start = time.Now()
	}
	return &WallClock{start: start, dur: tickDuration}
}
func (c *WallClock) Now() int64 { return c.t }
func (c *WallClock) Advance(d int64) {
	if d > 0 {
		c.t += d
	}
}

// WallTime returns the wall-clock instant corresponding to logical tick t.
func (c *WallClock) WallTime(t int64) time.Time { return c.start.Add(time.Duration(t) * c.dur) }
func (c *WallClock) Start() time.Time           { return c.start }
