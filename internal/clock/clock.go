package clock

import (
	"sort"
	"sync"
	"time"
)

// Clock abstracts time so shutdown phase timeouts can be driven deterministically
// in tests. Production code uses Real; tests use Fake and advance it explicitly.
type Clock interface {
	Now() time.Time
	After(d time.Duration) <-chan time.Time
}

// Real is the wall-clock implementation.
type Real struct{}

func (Real) Now() time.Time                         { return time.Now() }
func (Real) After(d time.Duration) <-chan time.Time { return time.After(d) }

type fakeTimer struct {
	deadline time.Time
	seq      uint64
	ch       chan time.Time
}

// Fake is a manually controlled clock. Timers only fire when Advance is called,
// so goroutines never experience spontaneous time movement.
type Fake struct {
	mu     sync.Mutex
	now    time.Time
	next   uint64
	timers []*fakeTimer
}

func NewFake(start time.Time) *Fake {
	return &Fake{now: start}
}

func (f *Fake) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.now
}

// After returns a buffered channel that receives t when the fake clock reaches
// the deadline. Safe to send under lock because the channel has capacity 1.
func (f *Fake) After(d time.Duration) <-chan time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	t := &fakeTimer{
		deadline: f.now.Add(d),
		seq:      f.next,
		ch:       make(chan time.Time, 1),
	}
	f.next++
	f.timers = append(f.timers, t)
	return t.ch
}

// Advance moves the clock forward and fires every due timer in deadline order.
func (f *Fake) Advance(d time.Duration) {
	f.mu.Lock()
	f.now = f.now.Add(d)
	due := make([]*fakeTimer, 0, len(f.timers))
	remaining := make([]*fakeTimer, 0, len(f.timers))
	for _, t := range f.timers {
		if !t.deadline.After(f.now) {
			due = append(due, t)
		} else {
			remaining = append(remaining, t)
		}
	}
	f.timers = remaining
	f.mu.Unlock()

	sort.Slice(due, func(i, j int) bool {
		if due[i].deadline.Equal(due[j].deadline) {
			return due[i].seq < due[j].seq
		}
		return due[i].deadline.Before(due[j].deadline)
	})
	for _, t := range due {
		t.ch <- t.deadline
	}
}
