// Package clock provides an injectable time source so lifecycle logic
// (expiry, redemption, transfer waits) can be tested without real sleeps.
package clock

import (
	"sync"
	"sync/atomic"
	"time"
)

// Clock is the time source used by all lifecycle logic.
type Clock interface {
	Now() time.Time
}

// Real is the wall clock (UTC).
type Real struct{}

func (Real) Now() time.Time { return time.Now().UTC() }

// Offset is the wall clock plus a mutable skew. It backs the optional
// admin "advance time" endpoint used for local time-travel demos.
type Offset struct {
	skewNanos atomic.Int64
}

func NewOffset() *Offset { return &Offset{} }

func (o *Offset) Now() time.Time { return time.Now().UTC().Add(time.Duration(o.skewNanos.Load())) }

func (o *Offset) Advance(d time.Duration) { o.skewNanos.Add(int64(d)) }

func (o *Offset) Skew() time.Duration { return time.Duration(o.skewNanos.Load()) }

// Fake is a manually advanced clock for tests.
type Fake struct {
	mu  sync.Mutex
	now time.Time
}

func NewFake(t time.Time) *Fake { return &Fake{now: t.UTC()} }

func (f *Fake) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.now
}

func (f *Fake) Advance(d time.Duration) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.now = f.now.Add(d)
}

func (f *Fake) Set(t time.Time) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.now = t.UTC()
}
