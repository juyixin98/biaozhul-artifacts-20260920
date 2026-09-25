package dynpool

import (
	"fmt"
)

// SubmitOption mutates submission behavior.
type SubmitOption func(*submitOpts)

type submitOpts struct {
	id string
}

// WithTaskID assigns a caller-chosen task id (must be unique within the pool).
// Without it the pool generates "task-N" ids.
func WithTaskID(id string) SubmitOption {
	return func(o *submitOpts) { o.id = id }
}

// Submit accepts a task for execution.
//
// If a worker immediately picks the task up it runs asynchronously; otherwise
// the task enters the bounded wait queue and an task.enqueued event is
// emitted. When the queue is full the configured RejectPolicy decides what
// happens, including possibly running the task on the caller's goroutine
// (PolicyCallerRun).
//
// The returned Handle is usable to wait for completion even when the task is
// rejected or later dropped by ShutdownNow.
func (p *Pool) Submit(fn TaskFunc, opts ...SubmitOption) (*Handle, error) {
	if fn == nil {
		return nil, ErrNilTask
	}
	var o submitOpts
	for _, opt := range opts {
		opt(&o)
	}

	p.mu.Lock()
	switch p.state {
	case StateShuttingDown, StateStopping:
		p.mu.Unlock()
		return nil, ErrPoolShuttingDown
	case StateStopped:
		p.mu.Unlock()
		return nil, ErrPoolStopped
	}
	if o.id == "" {
		p.idSeq++
		o.id = fmt.Sprintf("task-%d", p.idSeq)
	} else if _, dup := p.handles[o.id]; dup {
		p.mu.Unlock()
		return nil, ErrDuplicateTaskID
	}
	h := &Handle{ID: o.id, doneCh: make(chan struct{})}
	p.handles[o.id] = h
	p.taskWG.Add(1) // paired exactly once: runTask / rejectOne / ShutdownNow drop

	t := &task{id: o.id, fn: fn, h: h}
	// Enqueue while holding p.mu so that Shutdown, which makes its drainer
	// decision under the same lock, cannot miss a task accepted by a Submit
	// that raced with the state flip.
	queued := false
	select {
	case p.queue <- t:
		queued = true
	default:
	}
	p.mu.Unlock()

	p.emit(EventTaskSubmitted, 0, o.id, Event{})
	if queued {
		// A buffered channel hands the task straight to a blocked worker when
		// one is waiting; otherwise it sits in the wait queue.
		p.emit(EventTaskEnqueued, 0, o.id, Event{})
		return h, nil
	}
	return p.rejectTask(t)
}

// rejectTask applies the configured rejection policy when the queue is full.
func (p *Pool) rejectTask(t *task) (*Handle, error) {
	switch p.reject {
	case PolicyAbort:
		return p.discard(t, "queue full: abort", ErrTaskRejected)

	case PolicyDiscard:
		// Silent drop: no error returned, but the handle is done and a
		// task.rejected event records what happened.
		p.rejectOne(t, "queue full: discard")
		return t.h, nil

	case PolicyDiscardOldest:
		// Evict the oldest queued task, then enqueue the newcomer.
		select {
		case old := <-p.queue:
			p.rejectOne(old, "queue full: discard oldest")
		default:
			// Raced with a worker; the queue just freed up, fall through.
		}
		select {
		case p.queue <- t:
			p.emit(EventTaskEnqueued, 0, t.id, Event{})
			return t.h, nil
		default:
			return p.discard(t, "queue full: discard oldest (race)", ErrTaskRejected)
		}

	case PolicyCallerRun:
		// While the caller is executing the task it counts as a running task;
		// if the pool shuts down meanwhile graceful drain waits for it.
		if p.ctx.Err() != nil {
			return p.discard(t, "pool stopping: caller run", ErrPoolShuttingDown)
		}
		t.h.markStarted()
		p.running.Add(1)
		p.emit(EventTaskStarted, 0, t.id, Event{Reason: "caller run"})
		err := t.fn(p.ctx)
		p.running.Add(-1)
		p.completed.Add(1)
		canceled := p.ctx.Err() != nil
		t.h.markFinished(err)
		p.taskWG.Done()
		if canceled {
			p.canceledN.Add(1)
			p.emit(EventTaskCancelled, 0, t.id, Event{Reason: "caller run: context canceled"})
		}
		p.emit(EventTaskCompleted, 0, t.id, Event{Reason: "caller run"})
		return t.h, err
	}
	return p.discard(t, "unknown policy", ErrTaskRejected)
}

func (p *Pool) discard(t *task, reason string, retErr error) (*Handle, error) {
	p.rejectOne(t, reason)
	return t.h, retErr
}

func (p *Pool) rejectOne(t *task, reason string) {
	p.rejectedN.Add(1)
	t.h.markDiscarded()
	p.taskWG.Done()
	p.emit(EventTaskRejected, 0, t.id, Event{Reason: reason})
}

// Resize changes the desired worker count at runtime.
//
// Increase: new workers are started immediately.
// Decrease ("shrink"): workers are signalled to retire. A signalled worker
// finishes its current task and drains queued work before exiting; accepted
// tasks are never lost.
//
// Resize is rejected after shutdown has started; Shutdown drains to zero
// independently of the configured target.
func (p *Pool) Resize(n int) error {
	if n < 0 {
		return ErrInvalidSize
	}
	p.mu.Lock()
	switch p.state {
	case StateShuttingDown, StateStopping:
		p.mu.Unlock()
		return ErrPoolShuttingDown
	case StateStopped:
		p.mu.Unlock()
		return ErrPoolStopped
	}
	old := p.target
	if n == old {
		p.mu.Unlock()
		return nil
	}
	p.target = n

	var spawn []spawnSpec
	var retire []*workerState
	live := len(p.workers)
	if n > live {
		spawn = p.collectSpawnLocked(n - live)
	} else if n < live {
		// Pick arbitrary workers to retire. map iteration order is unspecified
		// but that is fine: every worker is equivalent.
		toRetire := live - n
		for id, st := range p.workers {
			if toRetire == 0 {
				break
			}
			delete(p.workers, id)
			if !st.retiring {
				st.retiring = true
				p.retiring++
				retire = append(retire, st)
				p.emitLocked(EventWorkerRetiring, st.id, "", Event{Reason: "resize"})
			}
			toRetire--
		}
	}
	p.mu.Unlock()

	p.emit(EventResize, 0, "", Event{OldSize: old, NewSize: n})
	for _, st := range retire {
		close(st.ret)
	}
	p.spawnAll(spawn)
	return nil
}

// SetWorkers is an alias for Resize matching the "thread count" terminology.
func (p *Pool) SetWorkers(n int) error { return p.Resize(n) }

// TaskCount returns the number of tasks currently executing.
func (p *Pool) TaskCount() int64 { return p.running.Load() }
