package pool

import (
	"context"
	"time"
)

// chanCtx adapts a "done" channel (the pool forceCh) into a context.Context,
// so running tasks receive context cancellation on ShutdownNow without the
// pool having to allocate one cancel func per task.
type chanCtx struct {
	parent context.Context
	done   <-chan struct{}
}

func (c chanCtx) Deadline() (time.Time, bool) { return c.parent.Deadline() }

func (c chanCtx) Done() <-chan struct{} { return c.done }

func (c chanCtx) Err() error {
	select {
	case <-c.done:
		return context.Canceled
	default:
		return c.parent.Err()
	}
}

func (c chanCtx) Value(key any) any { return c.parent.Value(key) }
