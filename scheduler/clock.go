package scheduler

import (
	"sort"
	"sync"
	"time"
)

// Clock abstracts time so the scheduler can be driven by a real clock in
// production and a manually advanced clock in tests.
type Clock interface {
	Now() time.Time
	// After returns a channel that receives the current time once the
	// given duration has elapsed on this clock.
	After(d time.Duration) <-chan time.Time
}

// RealClock is the production Clock backed by the time package.
type RealClock struct{}

func (RealClock) Now() time.Time                         { return time.Now() }
func (RealClock) After(d time.Duration) <-chan time.Time { return time.After(d) }

// ManualClock is a deterministic Clock for tests: time only moves when
// Advance is called.
type ManualClock struct {
	mu     sync.Mutex
	now    time.Time
	timers []*manualTimer
}

type manualTimer struct {
	at time.Time
	ch chan time.Time
}

func NewManualClock(start time.Time) *ManualClock {
	return &ManualClock{now: start}
}

func (c *ManualClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

// Pending reports how many timers are currently registered. Tests use it to
// wait until the scheduler and executor have armed their timers before
// advancing time.
func (c *ManualClock) Pending() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.timers)
}

func (c *ManualClock) After(d time.Duration) <-chan time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	t := &manualTimer{at: c.now.Add(d), ch: make(chan time.Time, 1)}
	c.timers = append(c.timers, t)
	return t.ch
}

// Advance moves the clock forward by d and fires every timer whose deadline
// falls at or before the new time, earliest first.
func (c *ManualClock) Advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.now = c.now.Add(d)
	var keep, fire []*manualTimer
	for _, t := range c.timers {
		if !t.at.After(c.now) {
			fire = append(fire, t)
		} else {
			keep = append(keep, t)
		}
	}
	c.timers = keep
	sort.Slice(fire, func(i, j int) bool { return fire[i].at.Before(fire[j].at) })
	for _, t := range fire {
		t.ch <- c.now
	}
}
