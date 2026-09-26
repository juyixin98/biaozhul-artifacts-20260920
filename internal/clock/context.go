package clock

import (
	"context"
	"sync"
	"time"
)

type clockCtx struct {
	parent   context.Context
	deadline time.Time
	done     chan struct{}

	mu  sync.Mutex
	err error
}

// WithTimeout returns a context whose deadline is driven by clk, not the wall
// clock. Advancing clk past the deadline cancels it; the returned CancelFunc
// and parent cancellation behave as usual. A timer left registered after an
// early cancel fires later as an idempotent no-op.
//
// The deadline timer is registered synchronously (before WithTimeout returns)
// so callers driving a virtual clock can never Advance past the deadline
// before the timer existed — that race would hang virtual-time tests.
func WithTimeout(parent context.Context, clk Clock, d time.Duration) (context.Context, context.CancelFunc) {
	c := &clockCtx{
		parent:   parent,
		deadline: clk.Now().Add(d),
		done:     make(chan struct{}),
	}
	after := clk.After(d)
	go func() {
		select {
		case <-after:
			c.finish(context.DeadlineExceeded)
		case <-parent.Done():
			c.finish(parent.Err())
		case <-c.done:
		}
	}()
	return c, func() { c.finish(context.Canceled) }
}

func (c *clockCtx) finish(err error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.err != nil {
		return
	}
	c.err = err
	close(c.done)
}

func (c *clockCtx) Deadline() (time.Time, bool) { return c.deadline, true }
func (c *clockCtx) Done() <-chan struct{}       { return c.done }

func (c *clockCtx) Err() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.err
}

func (c *clockCtx) Value(key any) any { return c.parent.Value(key) }
