package server

import (
	"context"
	"errors"
	"time"

	"dynpool/pool"
)

var errDemoTask = errors.New("demo task configured to fail")

// makeDemoTask builds a built-in task that sleeps (cooperatively interruptible
// by a force shutdown) and optionally fails. It lets the HTTP service drive
// real work without callers embedding Go code.
func makeDemoTask(id, typ string, sleep time.Duration, fail bool) pool.Task {
	return pool.Task{
		ID:   id,
		Type: typ,
		Fn: func(ctx context.Context) (any, error) {
			if sleep > 0 {
				t := time.NewTimer(sleep)
				defer t.Stop()
				select {
				case <-t.C:
				case <-ctx.Done():
					return nil, ctx.Err()
				}
			}
			if fail {
				return nil, errDemoTask
			}
			return map[string]any{"slept_ms": sleep.Milliseconds()}, nil
		},
	}
}
