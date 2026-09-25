package scheduler

import (
	"sort"
	"sync"
	"time"
)

// Clock abstracts time so scheduler logic is deterministic in tests.
type Clock interface {
	Now() time.Time
	// After calls fn after d of "clock time". With a FakeClock the
	// callback fires inside Advance; with a RealClock it fires in a
	// background goroutine when wall time elapses.
	After(d time.Duration, fn func())
}

// RealClock is wall-clock time backed by the time package.
type RealClock struct{}

func NewRealClock() *RealClock { return &RealClock{} }

func (RealClock) Now() time.Time { return time.Now() }

func (RealClock) After(d time.Duration, fn func()) {
	time.AfterFunc(d, fn)
}

type fakeTimer struct {
	at   time.Time
	seq  int64
	fn   func()
	done bool
}

// FakeClock is a manually advanced clock. Timer callbacks fire
// synchronously inside Advance, ordered by (deadline, insertion order),
// which makes simulations fully deterministic.
type FakeClock struct {
	mu      sync.Mutex
	now     time.Time
	nextSeq int64
	timers  []*fakeTimer
}

// NewFakeClock creates a fake clock at epoch (or the given start time).
func NewFakeClock(start ...time.Time) *FakeClock {
	t := time.Unix(0, 0).UTC()
	if len(start) > 0 {
		t = start[0]
	}
	return &FakeClock{now: t}
}

func (c *FakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

// After schedules fn. Durations <= 0 fire at the current instant on the
// next Advance call.
func (c *FakeClock) After(d time.Duration, fn func()) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.nextSeq++
	at := c.now
	if d > 0 {
		at = c.now.Add(d)
	}
	c.timers = append(c.timers, &fakeTimer{at: at, seq: c.nextSeq, fn: fn})
}

// Advance moves the clock forward d, firing every due timer exactly once,
// in (deadline, insertion order) order. Timers added by firing callbacks
// (e.g. a completion that schedules nothing here, but kept general) are
// themselves fired if due before Advance returns.
func (c *FakeClock) Advance(d time.Duration) {
	c.mu.Lock()
	c.now = c.now.Add(d)
	deadline := c.now
	c.mu.Unlock()

	for {
		c.mu.Lock()
		var due []*fakeTimer
		var pending []*fakeTimer
		for _, t := range c.timers {
			if !t.done && !t.at.After(deadline) {
				due = append(due, t)
			} else if !t.done {
				pending = append(pending, t)
			}
		}
		c.timers = pending
		c.mu.Unlock()

		if len(due) == 0 {
			return
		}
		sort.SliceStable(due, func(i, j int) bool {
			if due[i].at.Equal(due[j].at) {
				return due[i].seq < due[j].seq
			}
			return due[i].at.Before(due[j].at)
		})
		for _, t := range due {
			t.done = true
			t.fn()
		}
	}
}

// Pending reports how many timers have not fired yet (test helper).
func (c *FakeClock) Pending() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	n := 0
	for _, t := range c.timers {
		if !t.done {
			n++
		}
	}
	return n
}
