package dynpool

import (
	"sync/atomic"
	"time"
)

// TrackedExecutor is an Executor that counts launched goroutines. It is useful
// in tests to assert that workers never leak: after a (graceful or forced)
// shutdown the active count must be zero.
type TrackedExecutor struct {
	started atomic.Int64
	active  atomic.Int64
}

// Execute launches fn in a tracked goroutine.
func (e *TrackedExecutor) Execute(fn func()) {
	e.started.Add(1)
	e.active.Add(1)
	go func() {
		defer e.active.Add(-1)
		fn()
	}()
}

// Started returns the total number of goroutines ever launched.
func (e *TrackedExecutor) Started() int64 { return e.started.Load() }

// Active returns the number of goroutines currently running.
func (e *TrackedExecutor) Active() int64 { return e.active.Load() }

// WaitActive blocks until Active()==0 or the timeout elapses. It reports
// whether the count reached zero.
func (e *TrackedExecutor) WaitActive(timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if e.active.Load() == 0 {
			return true
		}
		time.Sleep(time.Millisecond)
	}
	return e.active.Load() == 0
}
