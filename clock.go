package batchagg

import (
	"sync"
	"time"
)

// Clock is the scheduler's view of time. Both wall-clock time and timers are
// accessed through it so that tests can replace time with a deterministic
// VirtualClock.
type Clock interface {
	Now() time.Time
	NewTimer(d time.Duration) Timer
}

// Timer mirrors the subset of time.Timer the scheduler relies on.
type Timer interface {
	// C returns the channel on which the timer expiry is delivered.
	C() <-chan time.Time
	// Stop prevents the timer from firing. It returns true if the call
	// stopped the timer before it expired, false if it had already expired
	// or been stopped.
	Stop() bool
	// Reset changes the timer's expiry to now+d. It returns true if the
	// timer was active, false if it had already expired or been stopped.
	Reset(d time.Duration) bool
}

// ---------------------------------------------------------------------------
// wall clock
// ---------------------------------------------------------------------------

// SystemClock is a Clock backed by the real time package.
type SystemClock struct{}

// NewSystemClock returns the wall-clock Clock implementation.
func NewSystemClock() SystemClock { return SystemClock{} }

func (SystemClock) Now() time.Time { return time.Now() }

func (SystemClock) NewTimer(d time.Duration) Timer {
	return &systemTimer{t: time.NewTimer(d)}
}

type systemTimer struct{ t *time.Timer }

func (s *systemTimer) C() <-chan time.Time { return s.t.C }
func (s *systemTimer) Stop() bool          { return s.t.Stop() }
func (s *systemTimer) Reset(d time.Duration) bool {
	return s.t.Reset(d)
}

// ---------------------------------------------------------------------------
// virtual clock
// ---------------------------------------------------------------------------

// VirtualClock is a manually advanced clock for deterministic tests. Timers
// never fire on their own: they fire when Advance moves the clock to or past
// their deadline. Timers with the same deadline fire in creation order, which
// matches Go's runtime behavior closely enough for the scheduler.
type VirtualClock struct {
	mu      sync.Mutex
	now     time.Time
	pending []*virtualTimer // min-heap by deadline, tie-broken by seq
	seq     uint64
}

// NewVirtualClock creates a virtual clock starting at the Unix epoch.
func NewVirtualClock() *VirtualClock {
	return NewVirtualClockAt(time.Unix(0, 0).UTC())
}

// NewVirtualClockAt creates a virtual clock starting at t.
func NewVirtualClockAt(t time.Time) *VirtualClock {
	return &VirtualClock{now: t}
}

// Now reports the virtual current time.
func (v *VirtualClock) Now() time.Time {
	v.mu.Lock()
	defer v.mu.Unlock()
	return v.now
}

// NewTimer returns a timer scheduled d after the current virtual time.
func (v *VirtualClock) NewTimer(d time.Duration) Timer {
	v.mu.Lock()
	defer v.mu.Unlock()
	v.seq++
	t := &virtualTimer{
		clock:    v,
		deadline: v.now.Add(d),
		seq:      v.seq,
		ch:       make(chan time.Time, 1),
	}
	v.heapPush(t)
	return t
}

// Advance moves the clock forward by d, firing every due timer in deadline
// order (including timers whose Reset/creation chains became due during the
// advance, as long as their deadline is not after the new current time).
// It returns the new virtual time.
func (v *VirtualClock) Advance(d time.Duration) time.Time {
	v.mu.Lock()
	target := v.now.Add(d)
	v.now = target
	for len(v.pending) > 0 && v.pending[0].deadline.Before(target.Add(1)) {
		t := v.heapPop()
		if t.stopped {
			continue
		}
		when := t.deadline
		if when.After(target) {
			when = target
		}
		t.fired = true
		// Buffered channel of size 1: send never blocks.
		t.ch <- when
	}
	v.mu.Unlock()
	return target
}

type virtualTimer struct {
	clock    *VirtualClock
	deadline time.Time
	seq      uint64
	ch       chan time.Time
	stopped  bool
	fired    bool
}

func (t *virtualTimer) C() <-chan time.Time { return t.ch }

func (t *virtualTimer) Stop() bool {
	t.clock.mu.Lock()
	defer t.clock.mu.Unlock()
	if t.stopped || t.fired {
		return false
	}
	t.stopped = true
	t.clock.heapRemove(t)
	return true
}

func (t *virtualTimer) Reset(d time.Duration) bool {
	t.clock.mu.Lock()
	defer t.clock.mu.Unlock()
	active := !t.stopped && !t.fired
	if active {
		t.clock.heapRemove(t)
	}
	t.clock.seq++
	t.seq = t.clock.seq
	t.deadline = t.clock.now.Add(d)
	t.stopped = false
	t.fired = false
	// Drain a previously delivered value if nobody consumed it (standard
	// time.Timer reset contract for an expired-but-unreceived timer).
	select {
	case <-t.ch:
	default:
	}
	t.clock.heapPush(t)
	return active
}

// --- min-heap over the pending slice (kept explicit to avoid heap.Init churn) ---

func (v *VirtualClock) heapPush(t *virtualTimer) {
	v.pending = append(v.pending, t)
	i := len(v.pending) - 1
	for i > 0 {
		parent := (i - 1) / 2
		if !v.timerLess(v.pending[i], v.pending[parent]) {
			break
		}
		v.pending[i], v.pending[parent] = v.pending[parent], v.pending[i]
		i = parent
	}
}

func (v *VirtualClock) heapPop() *virtualTimer {
	n := len(v.pending)
	top := v.pending[0]
	last := v.pending[n-1]
	v.pending = v.pending[:n-1]
	if n == 1 {
		return top
	}
	v.pending[0] = last
	i := 0
	for {
		l := 2*i + 1
		r := 2*i + 2
		smallest := i
		if l < len(v.pending) && v.timerLess(v.pending[l], v.pending[smallest]) {
			smallest = l
		}
		if r < len(v.pending) && v.timerLess(v.pending[r], v.pending[smallest]) {
			smallest = r
		}
		if smallest == i {
			break
		}
		v.pending[i], v.pending[smallest] = v.pending[smallest], v.pending[i]
		i = smallest
	}
	return top
}

func (v *VirtualClock) heapRemove(t *virtualTimer) {
	for i, p := range v.pending {
		if p == t {
			n := len(v.pending)
			v.pending[i] = v.pending[n-1]
			v.pending = v.pending[:n-1]
			if i < len(v.pending) {
				v.siftDown(i)
			}
			return
		}
	}
}

func (v *VirtualClock) siftDown(i int) {
	for {
		l := 2*i + 1
		r := 2*i + 2
		smallest := i
		if l < len(v.pending) && v.timerLess(v.pending[l], v.pending[smallest]) {
			smallest = l
		}
		if r < len(v.pending) && v.timerLess(v.pending[r], v.pending[smallest]) {
			smallest = r
		}
		if smallest == i {
			return
		}
		v.pending[i], v.pending[smallest] = v.pending[smallest], v.pending[i]
		i = smallest
	}
}

func (v *VirtualClock) timerLess(a, b *virtualTimer) bool {
	if !a.deadline.Equal(b.deadline) {
		return a.deadline.Before(b.deadline)
	}
	return a.seq < b.seq
}
