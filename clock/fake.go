package clock

import (
	"container/heap"
	"sync"
	"time"
)

// fakeTimer implements Timer over Fake.
type fakeTimer struct {
	ch      chan time.Time
	fired   bool
	stopped bool
	when    time.Time // zero once delivered/stopped
	index   int       // position in fake.timers, -1 when not tracked
	fc      *Fake
}

func (t *fakeTimer) C() <-chan time.Time { return t.ch }

// Stop removes the timer from the pending heap. It never drains the
// (size-1 buffered) channel: if Stop races a delivery the value stays
// readable, which is exactly the documented race behaviour of time.Timer.
func (t *fakeTimer) Stop() bool {
	t.fc.mu.Lock()
	defer t.fc.mu.Unlock()
	if t.stopped || t.fired {
		return false
	}
	t.stopped = true
	if t.index >= 0 {
		heap.Remove(&t.fc.timers, t.index)
		t.index = -1
	}
	return true
}

// Fake is a manually controlled Clock with deterministic timers.
//
// Time only moves when Advance (or Set) is called. Timers whose deadline is
// reached by a jump fire in deadline order, synchronously during the call,
// so a test that advances the clock and then inspects state is race-free.
type Fake struct {
	mu     sync.Mutex
	now    time.Time
	timers timerHeap
	seq    uint64 // breaks ties so insertion order is preserved
}

// NewFake returns a Fake clock anchored at t (time.Now() when t is zero).
func NewFake(t time.Time) *Fake {
	if t.IsZero() {
		t = time.Now()
	}
	return &Fake{now: t}
}

func (f *Fake) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.now
}

func (f *Fake) NewTimer(d time.Duration) Timer {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.newTimerLocked(d)
}

func (f *Fake) newTimerLocked(d time.Duration) *fakeTimer {
	t := &fakeTimer{
		// Buffer 1: delivery never blocks even if nobody reads yet, and a
		// value delivered before/around Stop remains observable.
		ch:    make(chan time.Time, 1),
		index: -1,
		fc:    f,
	}
	if d <= 0 {
		t.when = f.now
		t.fireLocked(f.now)
		return t
	}
	t.when = f.now.Add(d)
	f.seq++
	t.index = len(f.timers)
	heap.Push(&f.timers, fakeTimerItem{t: t, when: t.when, seq: f.seq})
	return t
}

// ArmTimer atomically stops old and installs a new timer scheduled for the
// absolute time at, all under the clock lock. If at is at or before the
// current (post-jump) time the new timer fires immediately, so a jump that
// lands concurrently cannot make this call miss a deadline.
func (f *Fake) ArmTimer(old Timer, at time.Time) Timer {
	f.mu.Lock()
	defer f.mu.Unlock()
	if ft, ok := old.(*fakeTimer); ok && ft.fc == f {
		if !ft.stopped && !ft.fired && ft.index >= 0 {
			heap.Remove(&f.timers, ft.index)
		}
		ft.index = -1
		ft.stopped = true
	}
	t := &fakeTimer{
		ch:    make(chan time.Time, 1),
		index: -1,
		fc:    f,
	}
	if !at.After(f.now) {
		// Already due (possibly because of a jump racing this install).
		t.when = f.now
		t.fireLocked(f.now)
	} else {
		t.when = at
		f.seq++
		t.index = len(f.timers)
		heap.Push(&f.timers, fakeTimerItem{t: t, when: at, seq: f.seq})
	}
	return t
}

// Advance moves the clock by d (d must be >= 0) and fires every timer whose
// deadline falls within [oldNow, newNow] in deadline order. It returns the
// number of timers that fired, which lets a test tell whether a waiting
// scheduler could have been woken by the jump (0 means no due work).
func (f *Fake) Advance(d time.Duration) int {
	if d < 0 {
		panic("clock.Fake.Advance: negative duration")
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	target := f.now.Add(d)
	var due []fakeTimerItem
	for len(f.timers) > 0 && !f.timers[0].when.After(target) {
		item := heap.Pop(&f.timers).(fakeTimerItem)
		item.t.index = -1
		due = append(due, item)
	}
	// Fire with the clock set to each timer's own deadline so handlers
	// observe a monotonic, plausible Now. Delivered in deadline (then seq)
	// order, so a cascade of zero-delay timers stays FIFO.
	for _, item := range due {
		if item.when.After(f.now) {
			f.now = item.when
		}
		item.t.fireLocked(item.when)
	}
	f.now = target
	return len(due)
}

// Set jumps the clock directly to t. Going backwards panics: scheduling
// math (ages, deadlines) assumes a monotonic clock. It fires every pending
// timer whose deadline is <= t, just like a forward Advance.
func (f *Fake) Set(t time.Time) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if t.Before(f.now) {
		panic("clock.Fake.Set: cannot move backwards")
	}
	var fired []fakeTimerItem
	for len(f.timers) > 0 && !f.timers[0].when.After(t) {
		item := heap.Pop(&f.timers).(fakeTimerItem)
		item.t.index = -1
		fired = append(fired, item)
	}
	for _, item := range fired {
		if item.when.After(f.now) {
			f.now = item.when
		}
		item.t.fireLocked(item.when)
	}
	f.now = t
}

// Pending reports how many armed timers currently exist (test helper).
func (f *Fake) Pending() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.timers)
}

func (t *fakeTimer) fireLocked(when time.Time) {
	if t.stopped || t.fired {
		return
	}
	t.fired = true
	t.index = -1
	select {
	case t.ch <- when:
	default:
	}
}

type fakeTimerItem struct {
	t    *fakeTimer
	when time.Time
	seq  uint64
}

type timerHeap []fakeTimerItem

func (h timerHeap) Len() int { return len(h) }
func (h timerHeap) Less(i, j int) bool {
	if h[i].when.Equal(h[j].when) {
		return h[i].seq < h[j].seq
	}
	return h[i].when.Before(h[j].when)
}
func (h timerHeap) Swap(i, j int) {
	h[i], h[j] = h[j], h[i]
	h[i].t.index = i
	h[j].t.index = j
}
func (h *timerHeap) Push(x any) { *h = append(*h, x.(fakeTimerItem)) }
func (h *timerHeap) Pop() any {
	old := *h
	n := len(old)
	it := old[n-1]
	*h = old[:n-1]
	return it
}
