// Package upstream is the in-process fake downstream service. No network and
// no production system is involved: its behaviour is fully scripted so tests
// can inject failures, latency and indefinite stalls on demand.
package upstream

import (
	"context"
	"errors"
	"sync"
	"time"

	"breakerhalfopen/internal/clock"
)

// ErrUpstreamFailure is the sentinel a failing fake call returns.
var ErrUpstreamFailure = errors.New("fake upstream: injected failure")

// Directive scripts one call: wait Delay (on the injected clock, interruptible
// by ctx), or when Stall is set block until explicitly released / canceled,
// then return ErrUpstreamFailure when Fail is set.
type Directive struct {
	Delay time.Duration `json:"delay"`
	Fail  bool          `json:"fail"`
	Stall bool          `json:"stall"`
}

// CallRecord is one finished fake call, for structured reports.
type CallRecord struct {
	ID     uint64    `json:"id"`
	Start  time.Time `json:"start"`
	End    time.Time `json:"end"`
	Result string    `json:"result"` // success | failure | canceled
}

// Upstream is the fake service.
type Upstream struct {
	clk clock.Clock

	mu       sync.Mutex
	script   []Directive
	pos      int
	fallback Directive
	dynamic  *Directive // when non-nil, overrides script/fallback for all calls
	active   int
	nextID   uint64
	stalled  map[uint64]chan struct{}
	records  []CallRecord
	cond     *sync.Cond
}

// New creates a fake upstream. fallback is served once the script is exhausted.
func New(clk clock.Clock, fallback Directive, script ...Directive) *Upstream {
	u := &Upstream{
		clk:      clk,
		script:   script,
		fallback: fallback,
		stalled:  make(map[uint64]chan struct{}),
	}
	u.cond = sync.NewCond(&u.mu)
	return u
}

// Call executes the next scripted directive.
func (u *Upstream) Call(ctx context.Context) error {
	u.mu.Lock()
	u.nextID++
	id := u.nextID
	d := u.nextDirectiveLocked()
	var release chan struct{}
	if d.Stall {
		release = make(chan struct{})
		u.stalled[id] = release
	}
	// Register any virtual-clock timer BEFORE advertising the call as active:
	// a test that waits for the active call and then advances the clock must
	// not race ahead of timer registration (which would hang the call).
	var delayCh <-chan time.Time
	if !d.Stall && d.Delay > 0 {
		delayCh = u.clk.After(d.Delay)
	}
	u.active++
	u.cond.Broadcast()
	start := u.clk.Now()
	u.mu.Unlock()

	var err error
	switch {
	case d.Stall:
		select {
		case <-release:
		case <-ctx.Done():
			err = ctx.Err()
		}
	case d.Delay <= 0:
		// Immediate directive: no timer was registered, callers do not need a
		// clock Advance to make progress.
	default:
		select {
		case <-delayCh:
		case <-ctx.Done():
			err = ctx.Err()
		}
	}

	result := "success"
	switch {
	case errors.Is(err, context.Canceled), errors.Is(err, context.DeadlineExceeded):
		result = "canceled"
		err = context.Canceled
	case err == nil && d.Fail:
		err = ErrUpstreamFailure
		result = "failure"
	case err == nil:
		err = nil
	}

	u.mu.Lock()
	u.active--
	delete(u.stalled, id)
	u.records = append(u.records, CallRecord{
		ID: id, Start: start, End: u.clk.Now(), Result: result,
	})
	u.cond.Broadcast()
	u.mu.Unlock()
	return err
}

func (u *Upstream) nextDirectiveLocked() Directive {
	if u.dynamic != nil {
		return *u.dynamic
	}
	if u.pos < len(u.script) {
		d := u.script[u.pos]
		u.pos++
		return d
	}
	return u.fallback
}

// SetBehavior installs a dynamic directive served to every subsequent call
// (used by the demo HTTP service). Pass nil to revert to script/fallback.
func (u *Upstream) SetBehavior(d *Directive) {
	u.mu.Lock()
	u.dynamic = d
	u.mu.Unlock()
}

// Behavior returns a copy of the active dynamic directive (nil when the
// script/fallback is in use).
func (u *Upstream) Behavior() *Directive {
	u.mu.Lock()
	defer u.mu.Unlock()
	if u.dynamic == nil {
		return nil
	}
	d := *u.dynamic
	return &d
}

// ReleaseStalled unblocks every stalled call so it returns its scripted
// result. Returns how many calls were released.
func (u *Upstream) ReleaseStalled() int {
	u.mu.Lock()
	chans := make([]chan struct{}, 0, len(u.stalled))
	for _, ch := range u.stalled {
		chans = append(chans, ch)
	}
	u.mu.Unlock()
	for _, ch := range chans {
		close(ch)
	}
	return len(chans)
}

// ReleaseOne unblocks a single stalled call (any one) and returns true; false
// when nothing is stalled. Useful for freeing exactly one half-open slot.
func (u *Upstream) ReleaseOne() bool {
	u.mu.Lock()
	var ch chan struct{}
	for _, c := range u.stalled {
		ch = c
		break
	}
	u.mu.Unlock()
	if ch == nil {
		return false
	}
	close(ch)
	return true
}

// WaitActiveN blocks until at least n calls are currently active.
func (u *Upstream) WaitActiveN(n int) {
	u.mu.Lock()
	defer u.mu.Unlock()
	for u.active < n {
		u.cond.Wait()
	}
}

// WaitIdle blocks until no calls are active.
func (u *Upstream) WaitIdle() {
	u.mu.Lock()
	defer u.mu.Unlock()
	for u.active > 0 {
		u.cond.Wait()
	}
}

// ActiveCalls returns how many calls are in flight right now.
func (u *Upstream) ActiveCalls() int {
	u.mu.Lock()
	defer u.mu.Unlock()
	return u.active
}

// StalledCalls returns IDs of calls blocked in the stall state.
func (u *Upstream) StalledCalls() []uint64 {
	u.mu.Lock()
	defer u.mu.Unlock()
	ids := make([]uint64, 0, len(u.stalled))
	for id := range u.stalled {
		ids = append(ids, id)
	}
	return ids
}

// Records returns a copy of finished-call records.
func (u *Upstream) Records() []CallRecord {
	u.mu.Lock()
	defer u.mu.Unlock()
	out := make([]CallRecord, len(u.records))
	copy(out, u.records)
	return out
}
