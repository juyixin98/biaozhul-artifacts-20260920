// Package clock provides a controllable time source. Production code uses
// Real; tests and the fault-injection harness can substitute Fake to make
// time-dependent behavior deterministic without contacting any external
// system.
package clock

import (
	"sync"
	"time"
)

// Clock abstracts time so tests can control it.
type Clock interface {
	Now() time.Time
	After(d time.Duration) <-chan time.Time
	Sleep(d time.Duration)
}

// Real is the wall-clock implementation.
type Real struct{}

func (Real) Now() time.Time                         { return time.Now() }
func (Real) After(d time.Duration) <-chan time.Time { return time.After(d) }
func (Real) Sleep(d time.Duration)                  { time.Sleep(d) }

type fakeTimer struct {
	deadline time.Time
	ch       chan time.Time
	fired    bool
}

// Fake is a manually advanced clock for deterministic tests.
type Fake struct {
	mu     sync.Mutex
	now    time.Time
	timers []*fakeTimer
}

// NewFake returns a Fake clock starting at the given instant.
func NewFake(start time.Time) *Fake {
	return &Fake{now: start}
}

func (f *Fake) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.now
}

func (f *Fake) After(d time.Duration) <-chan time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	ch := make(chan time.Time, 1)
	t := &fakeTimer{deadline: f.now.Add(d), ch: ch}
	f.timers = append(f.timers, t)
	f.fireLocked()
	return ch
}

func (f *Fake) Sleep(d time.Duration) {
	<-f.After(d)
}

// Advance moves the clock forward and fires any timers that came due.
func (f *Fake) Advance(d time.Duration) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.now = f.now.Add(d)
	f.fireLocked()
}

func (f *Fake) fireLocked() {
	kept := f.timers[:0]
	for _, t := range f.timers {
		if !t.fired && !t.deadline.After(f.now) {
			t.fired = true
			t.ch <- f.now
			continue
		}
		kept = append(kept, t)
	}
	f.timers = kept
}
