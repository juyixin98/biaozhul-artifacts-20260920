package clock

import (
	"sort"
	"sync"
	"time"
)

// Fake is a manually advanced Clock. Timers only fire when Advance moves the
// fake now past their deadline, which makes timeout logic deterministic.
type Fake struct {
	mu      sync.Mutex
	now     time.Time
	timers  map[int]*fakeTimer
	nextID  int
	changed chan struct{} // closed and replaced whenever now advances
}

type fakeTimer struct {
	deadline time.Time
	ch       chan time.Time
	fired    bool
	stopped  bool
}

func (t *fakeTimer) C() <-chan time.Time { return t.ch }

func (t *fakeTimer) Stop() bool {
	t.stopped = true
	return !t.fired
}

// NewFake returns a Fake clock starting at the given instant.
func NewFake(start time.Time) *Fake {
	return &Fake{
		now:     start,
		timers:  make(map[int]*fakeTimer),
		changed: make(chan struct{}),
	}
}

func (f *Fake) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.now
}

func (f *Fake) After(d time.Duration) <-chan time.Time {
	return f.NewTimer(d).C()
}

// Sleep blocks until the fake clock has been advanced by at least d.
func (f *Fake) Sleep(d time.Duration) {
	<-f.After(d)
}

func (f *Fake) NewTimer(d time.Duration) Timer {
	f.mu.Lock()
	defer f.mu.Unlock()
	t := &fakeTimer{
		deadline: f.now.Add(d),
		ch:       make(chan time.Time, 1),
	}
	f.timers[f.nextID] = t
	f.nextID++
	f.fireLocked()
	return t
}

// Pending reports how many timers are currently registered. Tests use it to
// synchronise with goroutines that sleep on the fake clock.
func (f *Fake) Pending() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.timers)
}

// Advance moves the fake clock forward by d, firing any timers whose
// deadline now passes (in deadline order).
func (f *Fake) Advance(d time.Duration) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.now = f.now.Add(d)
	f.fireLocked()
	close(f.changed)
	f.changed = make(chan struct{})
}

func (f *Fake) fireLocked() {
	ids := make([]int, 0, len(f.timers))
	for id, t := range f.timers {
		if !t.fired && !t.stopped && !t.deadline.After(f.now) {
			ids = append(ids, id)
		}
	}
	sort.Slice(ids, func(i, j int) bool {
		return f.timers[ids[i]].deadline.Before(f.timers[ids[j]].deadline)
	})
	for _, id := range ids {
		t := f.timers[id]
		t.fired = true
		t.ch <- t.deadline
		delete(f.timers, id)
	}
}
