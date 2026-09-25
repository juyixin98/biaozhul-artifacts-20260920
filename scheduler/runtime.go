package scheduler

import (
	"context"
	"fmt"
	"sync"
	"time"
)

// taskRuntime is the Runtime implementation handed to executing
// functions. "Helping" is the key property: when a task calls Wait or
// Sleep its worker does not block on a futex — it keeps popping and
// executing tasks, so a fixed pool of one worker can still run tasks
// that spawn children and wait for them (no thread-starvation
// deadlock).
type taskRuntime struct {
	exec   *Executor
	worker int
	task   *task
}

func (rt *taskRuntime) WorkerID() int  { return rt.worker }
func (rt *taskRuntime) Now() time.Time { return rt.exec.clock.Now() }

func (rt *taskRuntime) Spawn(ctx context.Context, kind string, payload any) (Handle, error) {
	tf, ok := rt.exec.lookupKind(kind)
	if !ok {
		return nil, fmt.Errorf("%w: %q", ErrKindNotFound, kind)
	}
	return rt.spawn(ctx, kind, asFunc(kind, tf, payload)), nil
}

func (rt *taskRuntime) SpawnFunc(ctx context.Context, name string, fn Func) (Handle, error) {
	if fn == nil {
		return nil, fmt.Errorf("%w: nil function", ErrInvalidArgument)
	}
	return rt.spawn(ctx, name, fn), nil
}

func (rt *taskRuntime) spawn(ctx context.Context, kind string, fn Func) Handle {
	e := rt.exec
	if ctx == nil {
		ctx = context.Background()
	}
	// Child contexts derive from the caller-provided context, so
	// caller cancellation propagates structurally.
	childCtx, childCancel := context.WithCancel(ctx)
	t := &task{
		kind:      kind,
		fn:        fn,
		ctx:       childCtx,
		cancel:    childCancel,
		state:     StatePending,
		done:      make(chan struct{}),
		children:  map[string]*task{},
		createdAt: e.clock.Now(),
	}

	var evs []Event
	e.mu.Lock()
	e.installTaskLocked(t, rt.task)
	e.outstanding++
	e.spawnCount++
	if childCtx.Err() != nil {
		// Caller context already canceled: finalize immediately and
		// never enqueue.
		evs = append(evs, e.makeEventLocked(EventSpawned, t, rt.worker, 0, nil))
		e.cancelSubtreeLocked([]*task{t}, &evs)
		for _, ev := range evs {
			e.deliverLocked(ev)
		}
		e.cv.Broadcast()
		e.mu.Unlock()
		return t
	}
	e.locals[rt.worker-1].pushBottom(t)
	e.deliverLocked(e.makeEventLocked(EventSpawned, t, rt.worker, 0, nil))
	e.cv.Broadcast()
	e.mu.Unlock()
	return t
}

// Wait blocks the current task until h is terminal, while this worker
// continues to execute available tasks (including h or its
// descendants). This keeps spawn/wait trees progressing with any pool
// size, including workers=1.
//
// The park step uses a classic condition-variable predicate loop: the
// terminal predicate is evaluated with the executor lock held and the
// goroutine waits on the same condition, so a broadcast cannot be
// missed. External context cancellation is bridged into a broadcast
// via context.AfterFunc.
func (rt *taskRuntime) Wait(ctx context.Context, h Handle) (any, error) {
	e := rt.exec
	t, err := e.asTask(h)
	if err != nil {
		return nil, err
	}
	if ctx == nil {
		ctx = context.Background()
	}
	stop := context.AfterFunc(ctx, func() {
		e.mu.Lock()
		e.cv.Broadcast()
		e.mu.Unlock()
	})
	defer stop()

	for {
		// Fast path: terminal or canceled?
		e.mu.Lock()
		if t.state.IsTerminal() {
			e.mu.Unlock()
			return rt.finishWait(t, ctx)
		}
		if err := ctx.Err(); err != nil {
			e.mu.Unlock()
			return nil, err
		}
		// Try to help with one task while holding no lock... but
		// cv.Wait must be called under the lock. Help is attempted
		// BEFORE parking on the condition: release, run one task if
		// present, otherwise re-lock and wait.
		e.mu.Unlock()

		if !e.helpOnce(rt.worker) {
			e.mu.Lock()
			// Re-check predicates under the lock (no lost wakeups).
			if t.state.IsTerminal() {
				e.mu.Unlock()
				return rt.finishWait(t, ctx)
			}
			if err := ctx.Err(); err != nil {
				e.mu.Unlock()
				return nil, err
			}
			e.cv.Wait()
			e.mu.Unlock()
		}
	}
}

func (rt *taskRuntime) finishWait(t *task, ctx context.Context) (any, error) {
	e := rt.exec
	e.mu.Lock()
	info := t.infoLocked()
	raw := t.rawErr
	e.mu.Unlock()
	if info.State == StateSucceeded {
		return info.Value, nil
	}
	if raw != nil {
		return info.Value, raw
	}
	switch info.State {
	case StatePanicked:
		return info.Value, fmt.Errorf("task %s panicked: %s", t.id, info.Err)
	case StateCanceled:
		if err := ctx.Err(); err != nil {
			return info.Value, err
		}
		return info.Value, context.Canceled
	case StateFailed:
		return info.Value, fmt.Errorf("task %s failed: %s", t.id, info.Err)
	}
	return info.Value, nil
}

// helpOnce executes at most one task visible to this worker.
func (e *Executor) helpOnce(worker int) bool {
	ran, _ := e.runOne(worker)
	return ran
}

// Sleep pauses the current task for d without pinning a worker:
// like Wait, the worker helps execute other tasks until the timer
// fires (or ctx is canceled). Timer firing and context cancellation
// are bridged into executor condvar broadcasts, and the park step
// re-evaluates predicates under the lock, so wakeups cannot be lost.
// Works identically with the real clock and a MockClock whose Advance
// fires timers synchronously from an external goroutine.
func (rt *taskRuntime) Sleep(ctx context.Context, d time.Duration) error {
	e := rt.exec
	tm := e.clock.NewTimer(d)

	// The fired/canceled predicates are guarded by the executor lock.
	// The bridge mutates them and broadcasts WHILE holding that lock;
	// the worker checks the predicates under the same lock
	// immediately before cv.Wait. That is the textbook condition-
	// variable protocol and makes wakeups unlosable.
	var fired, canceled bool

	bridgeDone := make(chan struct{})
	var bridgeWG sync.WaitGroup
	bridgeWG.Add(1)
	go func() {
		defer bridgeWG.Done()
		select {
		case <-tm.C():
			e.mu.Lock()
			fired = true
			e.cv.Broadcast()
			e.mu.Unlock()
		case <-ctx.Done():
			e.mu.Lock()
			canceled = true
			e.cv.Broadcast()
			e.mu.Unlock()
		case <-bridgeDone:
		}
	}()
	stopCtx := context.AfterFunc(ctx, func() {
		e.mu.Lock()
		canceled = true
		e.cv.Broadcast()
		e.mu.Unlock()
	})
	defer func() {
		stopCtx()
		close(bridgeDone)
		tm.Stop()
		e.mu.Lock()
		e.cv.Broadcast()
		e.mu.Unlock()
		bridgeWG.Wait()
	}()

	for {
		// Help with one available task before parking. Predicates are
		// read under the lock so the check cannot race the bridge's
		// locked state mutation.
		e.mu.Lock()
		shouldHelp := !fired && !canceled && ctx.Err() == nil
		e.mu.Unlock()
		if shouldHelp {
			e.helpOnce(rt.worker)
		}

		e.mu.Lock()
		for !fired && !canceled && ctx.Err() == nil {
			e.cv.Wait()
		}
		f, c := fired, canceled
		e.mu.Unlock()

		if f {
			return nil
		}
		if c || ctx.Err() != nil {
			return ctx.Err()
		}
	}
}
