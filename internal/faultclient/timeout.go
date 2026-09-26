package faultclient

import (
	"context"
	"time"

	"cancelprop/internal/clock"
)

type clockDeadlineCtx struct {
	context.Context
	deadline time.Time
}

func (c clockDeadlineCtx) Deadline() (time.Time, bool) { return c.deadline, true }

// withClockTimeout is context.WithTimeout driven by an arbitrary clock, so a
// FakeClock can trigger call timeouts with no real waiting. The returned
// stop function must always be invoked (deferred) to release the timer
// goroutine even when the parent is canceled first.
func withClockTimeout(parent context.Context, clk clock.Clock, timeout time.Duration) (context.Context, context.CancelFunc) {
	inner, cancel := context.WithCancel(parent)
	t := clk.NewTimer(timeout)
	go func() {
		select {
		case <-t.C():
			cancel()
		case <-inner.Done():
		}
	}()
	stop := func() {
		cancel()
		t.Stop()
	}
	return clockDeadlineCtx{Context: inner, deadline: clk.Now().Add(timeout)}, stop
}
