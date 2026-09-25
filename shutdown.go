package dynpool

import (
	"context"
)

// Shutdown performs a graceful shutdown.
//
// New submissions are rejected with ErrPoolShuttingDown. Every task already
// accepted (queued or running) is executed to completion with an uncancelled
// context; workers then stop. Shrink-selected workers that are still alive
// join the drain as usual. If the pool was resized to zero workers while
// tasks remained queued, internal drainer(s) process those tasks.
//
// Shutdown blocks until draining completes, ctx is cancelled, or the pool's
// GracePeriod elapses (whichever comes first). It is idempotent: calling
// Shutdown on an already-stopped pool returns nil; calling it while a
// ShutdownNow is in flight waits for the forced stop and returns
// ErrPoolShuttingDown.
func (p *Pool) Shutdown(ctx context.Context) error {
	// Fast-path observation; authoritative checks happen under both locks.
	p.mu.Lock()
	switch p.state {
	case StateStopped, StateShuttingDown:
		p.mu.Unlock()
		<-p.stoppedCh
		return nil
	case StateStopping:
		p.mu.Unlock()
		<-p.stoppedCh
		return ErrPoolShuttingDown
	}
	p.mu.Unlock()

	// Initiate the graceful drain while holding forceMu (exclusive with
	// ShutdownNow), but release it BEFORE blocking on the drain: otherwise a
	// later ShutdownNow could never acquire the lock to cancel blocked tasks,
	// deadlocking the graceful waiter itself.
	p.forceMu.Lock()
	p.mu.Lock()
	if p.state != StateRunning {
		// ShutdownNow or another Shutdown raced in.
		forced := p.state == StateStopping
		p.mu.Unlock()
		p.forceMu.Unlock()
		<-p.stoppedCh
		if forced {
			return ErrPoolShuttingDown
		}
		return nil
	}
	p.state = StateShuttingDown
	old := p.target
	p.target = 0
	retire := p.collectRetireLocked()
	for _, st := range retire {
		p.emitLocked(EventWorkerRetiring, st.id, "", Event{Reason: "shutdown"})
	}
	needDrainer := p.active.Load() == 0 && len(p.queue) > 0
	p.mu.Unlock()

	p.emit(EventPoolShutdown, 0, "", Event{OldSize: old, NewSize: 0})
	for _, st := range retire {
		close(st.ret)
	}
	if needDrainer {
		p.wg.Add(1)
		p.executor.Execute(p.drainer)
	}
	p.forceMu.Unlock()

	if p.grace > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, p.grace)
		defer cancel()
	}
	return p.waitDrain(ctx)
}

// waitDrain blocks until both all accepted tasks have completed and all
// worker goroutines have exited, or ctx expires first. The pool always
// transitions to stopped in the background once the drain finishes, even if
// the caller gave up waiting at the deadline. If a ShutdownNow escalates the
// shutdown while waiting, ErrPoolShuttingDown is returned.
func (p *Pool) waitDrain(ctx context.Context) error {
	go p.finishWhenDrained()
	select {
	case <-p.stoppedCh:
		if p.forced.Load() {
			return ErrPoolShuttingDown
		}
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// finishWhenDrained performs the one-time transition to stopped. The emitted
// event reflects how the pool actually stopped: force_stopped if the state was
// escalated to "stopping" (ShutdownNow) while a graceful drain was pending.
func (p *Pool) finishWhenDrained() {
	p.taskWG.Wait()
	p.wg.Wait()
	p.stopOnce.Do(p.transitionStopped)
}

// ShutdownNow performs a forced shutdown.
//
//   - New submissions are rejected.
//   - The context handed to every running task is cancelled immediately;
//     cooperative blocking tasks should return promptly.
//   - All tasks still waiting in the queue are removed and their ids returned;
//     they never execute.
//
// ShutdownNow blocks until all workers have observed the cancellation and
// returned (tasks must cooperate with context cancellation). It is idempotent:
// a second call returns nil.
func (p *Pool) ShutdownNow() []string {
	p.forceMu.Lock()
	defer p.forceMu.Unlock()

	p.mu.Lock()
	if p.state == StateStopped {
		p.mu.Unlock()
		return nil
	}
	p.state = StateStopping
	p.forced.Store(true)
	p.target = 0
	retire := p.collectRetireLocked()
	for _, st := range retire {
		p.emitLocked(EventWorkerRetiring, st.id, "", Event{Reason: "force"})
	}
	p.mu.Unlock()

	p.cancelCtx()
	for _, st := range retire {
		close(st.ret)
	}

	// Wait for workers first: a worker that raced a queued receive with the
	// cancellation drops that task itself (exactly once).
	p.wg.Wait()

	// Drain anything still in the wait queue: these accepted tasks never run.
	for {
		select {
		case t := <-p.queue:
			p.dropTask(0, t)
		default:
			p.dropMu.Lock()
			dropped := make([]string, len(p.droppedList))
			copy(dropped, p.droppedList)
			p.dropMu.Unlock()
			p.stopOnce.Do(p.transitionStopped)
			return dropped
		}
	}
}

// collectRetireLocked removes every still-registered worker from the registry
// and marks it retiring. Caller holds p.mu.
func (p *Pool) collectRetireLocked() []*workerState {
	out := make([]*workerState, 0, len(p.workers))
	for id, st := range p.workers {
		delete(p.workers, id)
		if !st.retiring {
			st.retiring = true
			p.retiring++
		}
		out = append(out, st)
	}
	return out
}

// transitionStopped moves the pool to StateStopped and emits the matching
// terminal event (called exactly once via stopOnce).
func (p *Pool) transitionStopped() {
	p.mu.Lock()
	from := p.state
	p.state = StateStopped
	p.target = 0
	p.retiring = 0
	p.mu.Unlock()
	if from == StateStopping {
		p.emit(EventPoolForceStop, 0, "", Event{Reason: "force"})
	} else {
		p.emit(EventPoolStopped, 0, "", Event{Reason: "graceful"})
	}
	close(p.stoppedCh)
}

// Stopped returns a channel closed once the pool has fully stopped.
func (p *Pool) Stopped() <-chan struct{} { return p.stoppedCh }
