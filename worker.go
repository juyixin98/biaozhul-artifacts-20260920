package dynpool

import (
	"context"
	"errors"
)

// workerState is the pool-side record of one live worker.
type workerState struct {
	id       int64
	ret      chan struct{}
	retiring bool // selected to leave (counted in p.retiring)
}

// spawnSpec pairs a freshly assigned worker id with its state.
type spawnSpec struct {
	id int64
	st *workerState
}

// collectSpawnLocked reserves n new worker ids. Caller holds p.mu.
func (p *Pool) collectSpawnLocked(n int) []spawnSpec {
	out := make([]spawnSpec, 0, n)
	for i := 0; i < n; i++ {
		p.idSeq++
		st := &workerState{id: p.idSeq, ret: make(chan struct{})}
		p.workers[p.idSeq] = st
		out = append(out, spawnSpec{p.idSeq, st})
	}
	return out
}

// spawnAll starts the reserved workers. wg.Add and the executor launch happen
// outside p.mu so a slow Executor cannot block pool operations.
func (p *Pool) spawnAll(spawn []spawnSpec) {
	for _, s := range spawn {
		s := s
		p.wg.Add(1)
		p.executor.Execute(func() { p.worker(s.st) })
	}
}

// worker is the per-worker loop.
//
// Each loop iteration waits for either a queued task, a retirement signal,
// shutdown, or force cancellation. A retire signal does NOT interrupt a
// running task: the in-flight task is executed normally. After it, a retiring
// worker keeps draining the queue until empty. That is what guarantees a
// shrink cannot strand accepted work.
//
// Retirement bookkeeping (p.retiring / worker.retiring) happens when the pool
// signals the worker, not when the worker notices — so a blocked worker
// running a long task is visibly "retiring" immediately.
func (p *Pool) worker(st *workerState) {
	p.active.Add(1)
	p.emit(EventWorkerStarted, st.id, "", Event{})
	defer func() {
		p.active.Add(-1)
		p.mu.Lock()
		if st.retiring {
			p.retiring--
		}
		p.mu.Unlock()
		p.emit(EventWorkerExited, st.id, "", Event{})
		p.wg.Done()
	}()

	shouldRetire := false
	for {
		select {
		case <-p.ctx.Done(): // force stop
			return
		case <-st.ret:
			shouldRetire = true
		case t := <-p.queue:
			// A receive racing with ShutdownNow's context cancellation may
			// land here: do not run the task, drop it instead so it is
			// accounted exactly once.
			if p.ctx.Err() != nil {
				p.dropTask(st.id, t)
				return
			}
			p.runTask(st.id, t)
		}

		// A retiring worker only leaves once the queue is drained (or force
		// stop is underway).
		if shouldRetire {
			if p.ctx.Err() != nil {
				return
			}
			select {
			case t := <-p.queue:
				if p.ctx.Err() != nil {
					p.dropTask(st.id, t)
					return
				}
				p.runTask(st.id, t)
			default:
				return // queue empty: safe to leave
			}
		}
	}
}

// drainer is a temporary worker used during graceful shutdown when the pool
// reached zero live workers while tasks were still queued (possible when
// target was resized to 0 right before Shutdown). It drains queued tasks in
// FIFO order until the queue is empty, then exits. It is interruptible by
// ShutdownNow.
func (p *Pool) drainer() {
	p.active.Add(1)
	p.emit(EventWorkerStarted, 0, "", Event{Reason: "drainer"})
	defer func() {
		p.active.Add(-1)
		p.emit(EventWorkerExited, 0, "", Event{Reason: "drainer"})
		p.wg.Done()
	}()
	for {
		select {
		case <-p.ctx.Done():
			return
		case t := <-p.queue:
			if p.ctx.Err() != nil {
				p.dropTask(0, t)
				return
			}
			p.runTask(0, t)
		default:
			return // queued work is drained; nothing else can arrive (submit rejected)
		}
	}
}

// dropTask records an unstarted task that never will run because of a forced
// shutdown. Channel receives are exclusive, so each queued task is dropped by
// exactly one party (a worker or ShutdownNow's own drain).
func (p *Pool) dropTask(workerID int64, t *task) {
	p.droppedN.Add(1)
	p.taskWG.Done()
	t.h.markDiscarded()
	p.dropMu.Lock()
	p.droppedList = append(p.droppedList, t.id)
	p.dropMu.Unlock()
	p.emit(EventTaskDropped, workerID, t.id, Event{Reason: "force shutdown"})
}

// runTask executes one accepted task exactly once.
func (p *Pool) runTask(workerID int64, t *task) {
	t.h.markStarted()
	p.running.Add(1)
	p.emit(EventTaskStarted, workerID, t.id, Event{})

	err := t.fn(p.ctx)

	p.running.Add(-1)
	p.completed.Add(1)
	canceled := errors.Is(err, context.Canceled) || p.ctx.Err() != nil
	t.h.markFinished(err)
	p.taskWG.Done()
	ev := Event{}
	if canceled {
		p.canceledN.Add(1)
		ev.Reason = "context canceled"
		p.emit(EventTaskCancelled, workerID, t.id, ev)
	}
	p.emit(EventTaskCompleted, workerID, t.id, ev)
}
