package clock

import (
	"context"
	"sync"
	"time"
)

// waiter is a single pending deadline: a Timer or a deadline context.
type waiter struct {
	when  time.Time
	seq   int
	sched bool // true for scheduler-internal wake timers (drivers handle these separately)
	fire  func()
}

// FakeClock is a manually driven clock for deterministic tests.
//
// Simulated time only moves when a test calls Fire* methods. Waiters are
// kept in chronological order and fired one at a time; between firings the
// driver drains its scheduler actor and executor goroutines so every cascade
// triggered by a firing is observed before the next deadline.
//
// Two waiter kinds exist:
//
//   - ordinary waiters (NewTimer, DeadlineContext) represent work that blocks
//     an executor goroutine — scripted sleeps and a job's kill-bound context.
//     FireNext advances through these, and after each firing the driver can
//     wait for the executor to deliver its result.
//   - scheduler waiters (NewSchedulerTimer) are internal bookkeeping, e.g.
//     the timer that expires queued jobs past their deadline. FireSchedulerWake
//     fires the earliest such timer when it is due.
//
// All exported methods are safe for concurrent use.
type FakeClock struct {
	mu      sync.Mutex
	now     time.Time
	waiters []*waiter // pending ordinary, sorted by (when, seq)
	scheds  []*waiter // pending scheduler-internal, sorted by (when, seq)
	seq     int
}

// NewFakeClock returns a fake clock anchored at the Unix epoch.
func NewFakeClock() *FakeClock { return NewFakeClockAt(time.Unix(0, 0)) }

// NewFakeClockAt returns a fake clock anchored at t.
func NewFakeClockAt(t time.Time) *FakeClock {
	return &FakeClock{now: t}
}

func (f *FakeClock) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.now
}

// insertLocked adds w to the given bucket in (when, seq) order.
func insertLocked(bucket *[]*waiter, w *waiter) {
	i := 0
	for i < len(*bucket) && !w.when.Before((*bucket)[i].when) {
		i++
	}
	*bucket = append(*bucket, nil)
	copy((*bucket)[i+1:], (*bucket)[i:])
	(*bucket)[i] = w
}

// register stamps a sequence and inserts a waiter.
func (f *FakeClock) register(w *waiter) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.seq++
	w.seq = f.seq
	if w.when.Before(f.now) {
		w.when = f.now
	}
	if w.sched {
		insertLocked(&f.scheds, w)
	} else {
		insertLocked(&f.waiters, w)
	}
}

func (f *FakeClock) remove(w *waiter) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	bucket := &f.waiters
	if w.sched {
		bucket = &f.scheds
	}
	for i, x := range *bucket {
		if x == w {
			*bucket = append((*bucket)[:i], (*bucket)[i+1:]...)
			return true
		}
	}
	return false
}

// WaiterCount reports the number of pending ordinary waiters.
func (f *FakeClock) WaiterCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.waiters)
}

// PeekNext returns the earliest pending ordinary deadline.
func (f *FakeClock) PeekNext() (time.Time, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.waiters) == 0 {
		return time.Time{}, false
	}
	return f.waiters[0].when, true
}

// fireHead pops and fires the head of bucket if due.
func (f *FakeClock) fireHead(bucket *[]*waiter, until time.Time) (bool, time.Time) {
	f.mu.Lock()
	if len(*bucket) == 0 || (*bucket)[0].when.After(until) {
		f.mu.Unlock()
		return false, time.Time{}
	}
	w := (*bucket)[0]
	*bucket = (*bucket)[1:]
	if w.when.After(f.now) {
		f.now = w.when
	}
	fire := w.fire
	at := w.when
	f.mu.Unlock()

	fire()
	return true, at
}

// FireNext fires the earliest ordinary (executor-blocking) waiter due by
// until, advancing the clock to it. Scheduler-internal timers are skipped.
// Returns false when nothing ordinary is due.
func (f *FakeClock) FireNext(until time.Time) bool {
	fired, _ := f.fireHead(&f.waiters, until)
	return fired
}

// FireSchedulerWake fires the earliest scheduler-internal waiter due by until,
// advancing the clock to it. Returns false when none is due.
func (f *FakeClock) FireSchedulerWake(until time.Time) bool {
	fired, _ := f.fireHead(&f.scheds, until)
	return fired
}

// PeekScheduler returns the earliest scheduler-internal pending deadline.
func (f *FakeClock) PeekScheduler() (time.Time, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.scheds) == 0 {
		return time.Time{}, false
	}
	return f.scheds[0].when, true
}

// FireAllDue fires every pending ordinary and scheduler waiter due by until,
// in chronological order, advancing the clock.
func (f *FakeClock) FireAllDue(until time.Time) int {
	n := 0
	for {
		// Pick the earliest due waiter across both buckets.
		f.mu.Lock()
		var wo, ws *waiter
		if len(f.waiters) > 0 && !f.waiters[0].when.After(until) {
			wo = f.waiters[0]
		}
		if len(f.scheds) > 0 && !f.scheds[0].when.After(until) {
			ws = f.scheds[0]
		}
		var pick *waiter
		bucket := &f.waiters
		switch {
		case wo == nil && ws == nil:
			f.mu.Unlock()
			return n
		case wo == nil:
			pick, bucket = ws, &f.scheds
		case ws == nil:
			pick = wo
		default:
			if wo.when.Before(ws.when) {
				pick = wo
			} else if ws.when.Before(wo.when) {
				pick, bucket = ws, &f.scheds
			} else {
				// Same instant: ordinary (executor) first.
				pick = wo
			}
		}
		*bucket = (*bucket)[1:]
		if pick.when.After(f.now) {
			f.now = pick.when
		}
		fire := pick.fire
		f.mu.Unlock()
		fire()
		n++
	}
}

// AdvanceTo jumps the clock to t without firing anything.
func (f *FakeClock) AdvanceTo(t time.Time) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if t.After(f.now) {
		f.now = t
	}
}

// ---- Timer ----

type fakeTimer struct {
	fc   *FakeClock
	w    *waiter
	ch   chan time.Time
	done bool
	mu   sync.Mutex
}

func (ft *fakeTimer) C() <-chan time.Time { return ft.ch }

func (ft *fakeTimer) Stop() bool {
	ft.mu.Lock()
	defer ft.mu.Unlock()
	if ft.done {
		return false
	}
	ft.done = true
	return ft.fc.remove(ft.w)
}

func (f *FakeClock) newTimer(d time.Duration, sched bool) *fakeTimer {
	ft := &fakeTimer{fc: f, ch: make(chan time.Time, 1)}
	now := f.Now()
	w := &waiter{when: now.Add(d), sched: sched}
	w.fire = func() {
		ft.mu.Lock()
		already := ft.done
		ft.done = true
		ft.mu.Unlock()
		if already {
			return
		}
		select {
		case ft.ch <- w.when:
		default:
		}
	}
	ft.w = w
	f.register(w)
	return ft
}

// NewTimer creates an ordinary (executor-visible) timer that fires after d.
func (f *FakeClock) NewTimer(d time.Duration) Timer {
	return f.newTimer(d, false)
}

// NewSchedulerTimer creates an internal wake timer for scheduler bookkeeping.
// Deterministic drivers handle these via FireSchedulerWake / FireAllDue.
func (f *FakeClock) NewSchedulerTimer(d time.Duration) Timer {
	return f.newTimer(d, true)
}

// After is provided for interface completeness; scheduler code uses timers.
func (f *FakeClock) After(d time.Duration) <-chan time.Time {
	return f.NewTimer(d).C()
}

// ---- deadline context ----

type fakeCtx struct {
	fc       *FakeClock
	parent   context.Context
	deadline time.Time
	w        *waiter

	mu    sync.Mutex
	err   error
	cause error
	done  chan struct{}
}

func (c *fakeCtx) Deadline() (time.Time, bool) { return c.deadline, true }
func (c *fakeCtx) Done() <-chan struct{}       { return c.done }
func (c *fakeCtx) Err() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.err
}
func (c *fakeCtx) Cause() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.cause != nil {
		return c.cause
	}
	return c.err
}
func (c *fakeCtx) Value(key any) any { return c.parent.Value(key) }

func (c *fakeCtx) cancel(err, cause error) bool {
	c.mu.Lock()
	if c.err != nil {
		c.mu.Unlock()
		return false
	}
	c.err = err
	c.cause = cause
	close(c.done)
	c.mu.Unlock()
	c.fc.remove(c.w)
	return true
}

// DeadlineContext returns a context canceled when the fake clock reaches t.
// It is an ordinary waiter: job kill-bounds are events drivers deliberately
// advance through.
func (f *FakeClock) DeadlineContext(parent context.Context, t time.Time) (context.Context, context.CancelFunc) {
	c := &fakeCtx{fc: f, parent: parent, deadline: t, done: make(chan struct{})}
	w := &waiter{
		when: t,
		fire: func() { c.cancel(context.DeadlineExceeded, context.DeadlineExceeded) },
	}
	c.w = w
	f.register(w)

	if pDone := parent.Done(); pDone != nil {
		go func() {
			select {
			case <-pDone:
				c.cancel(parent.Err(), context.Cause(parent))
			case <-c.done:
			}
		}()
	}
	return c, func() { c.cancel(context.Canceled, context.Canceled) }
}
