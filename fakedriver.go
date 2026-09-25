package deadlineadm

import (
	"time"

	"deadlineadm/clock"
)

// finishWaiter is implemented by executors that can signal completion of an
// individual script goroutine (the script executor used with fake clocks).
type finishWaiter interface {
	WaitFinished(d time.Duration) bool
}

// FakeDriver deterministically drives a Scheduler built on a clock.FakeClock.
// It is intended for tests and demos: each step advances simulated time to the
// earliest pending event (an executor completion or a scheduler wake) and
// drains the resulting cascades before stepping again.
//
// Single-goroutine use: submit and cancel jobs between steps; do not drive
// concurrently from multiple goroutines.
type FakeDriver struct {
	Sched *Scheduler
	Clk   *clock.FakeClock
	fw    finishWaiter
}

// NewFakeDriver wires a driver to a scheduler/fake-clock pair. When exec also
// satisfies the single-completion waiter interface (the script executor does),
// the driver can deterministically await exactly the goroutine each event
// unblocks; otherwise it falls back to actor barriers alone.
func NewFakeDriver(s *Scheduler, c *clock.FakeClock) *FakeDriver {
	d := &FakeDriver{Sched: s, Clk: c}
	if fw, ok := s.exec.(finishWaiter); ok {
		d.fw = fw
	}
	return d
}

// peekAndFire fires the single earliest due event, preferring an executor
// event at an equal instant. Returns (fired, executorEvent).
func (d *FakeDriver) peekAndFire(t time.Time) (bool, bool) {
	ord, hasOrd := d.Clk.PeekNext()
	sch, hasSch := d.Clk.PeekScheduler()
	switch {
	case hasOrd && (!hasSch || !ord.After(sch)):
		if ord.After(t) {
			if hasSch && !sch.After(t) {
				return d.Clk.FireSchedulerWake(t), false
			}
			return false, false
		}
		return d.Clk.FireNext(t), true
	case hasSch:
		if sch.After(t) {
			return false, false
		}
		return d.Clk.FireSchedulerWake(t), false
	default:
		return false, false
	}
}

// drainExec waits for the executor goroutine unblocked by the latest firing to
// hand its result to the actor, then runs the actor to finish the cascade. A
// bounded wait means a stale timer (whose owner already ended) cannot stall.
func (d *FakeDriver) drainExec() {
	if d.fw != nil {
		d.fw.WaitFinished(200 * time.Millisecond)
	}
	d.Sched.sync()
}

func (d *FakeDriver) drainSched() {
	d.Sched.sync()
	if d.Sched.killsPending() {
		d.Sched.waitKills()
		d.Sched.sync()
	}
}

// settleOnce processes all events due by t, draining executor and actor after
// each one, until no further event is due.
func (d *FakeDriver) settleOnce(t time.Time) {
	for {
		fired, isExec := d.peekAndFire(t)
		if !fired {
			// A prior firing may have scheduled a cascade that registers a new
			// due event; barrier once and re-check before giving up.
			d.Sched.sync()
			fired2, isExec2 := d.peekAndFire(t)
			if !fired2 {
				return
			}
			if isExec2 {
				d.drainExec()
			} else {
				d.drainSched()
			}
			continue
		}
		if isExec {
			d.drainExec()
		} else {
			d.drainSched()
		}
	}
}

// Advance moves simulated time to t (even when no timer is due at exactly t)
// and settles all cascades up to it.
func (d *FakeDriver) Advance(t time.Time) {
	d.Clk.AdvanceTo(t)
	d.settleOnce(t)
}

// AdvanceMS advances by ms simulated milliseconds.
func (d *FakeDriver) AdvanceMS(ms int64) {
	target := d.Clk.Now().Add(time.Duration(ms) * time.Millisecond)
	d.Clk.AdvanceTo(target)
	d.settleOnce(target)
}

// RunUntil keeps stepping to the earliest event until none remains before the
// limit, i.e. the schedule has fully settled.
func (d *FakeDriver) RunUntil(limit time.Time) {
	for {
		at, ok := d.nextEvent()
		if !ok || at.After(limit) {
			return
		}
		d.settleOnce(at)
	}
}

// RunToEnd steps until no event remains, bounded by limitMs simulated ms.
func (d *FakeDriver) RunToEnd(limitMs int64) {
	d.RunUntil(d.Clk.Now().Add(time.Duration(limitMs) * time.Millisecond))
}

func (d *FakeDriver) nextEvent() (time.Time, bool) {
	ord, okOrd := d.Clk.PeekNext()
	sch, okSch := d.Clk.PeekScheduler()
	switch {
	case okOrd && okSch:
		if sch.Before(ord) {
			return sch, true
		}
		return ord, true
	case okOrd:
		return ord, true
	case okSch:
		return sch, true
	default:
		return time.Time{}, false
	}
}
