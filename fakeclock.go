package tailsampling

import (
	"sort"
	"sync"
	"time"
)

// fakeClock is a manually-advanced Clock for deterministic tests. Timers do
// not fire until Advance moves past their deadline. Tickers are minimal:
// tests drive snapshots explicitly (SnapshotInterval=0).
type fakeClock struct {
	mu     sync.Mutex
	now    time.Time
	nextID int64
	timers map[int64]*fakeTimer
}

type fakeTimer struct {
	id       int64
	deadline time.Time
	fn       func()
	stopped  bool
	clock    *fakeClock
}

type fakeTicker struct{ ch chan time.Time }

func NewFakeClock(start time.Time) *fakeClock {
	return &fakeClock{now: start, timers: map[int64]*fakeTimer{}}
}

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *fakeClock) AfterFunc(d time.Duration, f func()) Timer {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.nextID++
	t := &fakeTimer{
		id:       c.nextID,
		deadline: c.now.Add(d),
		fn:       f,
		clock:    c,
	}
	c.timers[t.id] = t
	return t
}

func (c *fakeClock) NewTicker(time.Duration) Ticker {
	return &fakeTicker{ch: make(chan time.Time, 1)}
}

func (t *fakeTimer) Stop() bool {
	t.clock.mu.Lock()
	defer t.clock.mu.Unlock()
	wasActive := !t.stopped
	t.stopped = true
	delete(t.clock.timers, t.id)
	return wasActive
}

// Advance moves the clock and fires every due timer exactly once.
func (c *fakeClock) Advance(d time.Duration) {
	c.mu.Lock()
	c.now = c.now.Add(d)
	var dueFns []func()
	for id, t := range c.timers {
		if !t.stopped && !t.deadline.After(c.now) {
			dueFns = append(dueFns, t.fn)
			delete(c.timers, id)
			t.stopped = true
		}
	}
	c.mu.Unlock()

	// Fire outside the clock lock. Finalization callbacks only send to a
	// buffered channel, so they never block on the aggregator loop; tests
	// follow Advance with Aggregator.WaitIdle to observe the processing.
	sort.SliceStable(dueFns, func(i, j int) bool { return i < j })
	for _, fn := range dueFns {
		fn()
	}
}

func (tk *fakeTicker) Stop()               { close(tk.ch) }
func (tk *fakeTicker) C() <-chan time.Time { return tk.ch }
