package scheduler

import (
	"context"
	"strings"
	"sync"
	"time"
)

// nodeRuntime holds mutable per-node state, guarded by Job.mu.
type nodeRuntime struct {
	spec        *NodeSpec
	policy      DepPolicy
	maxAttempts int

	state     NodeState
	attempts  int // total Execute calls started
	remaining int // unfinished dependency count
	lastErr   string
	startedAt time.Time
	endedAt   time.Time
}

type reportKind int

const (
	reportFinished reportKind = iota // attempt returned nil
	reportFailed                     // attempt returned err
)

type nodeReport struct {
	node string
	kind reportKind
	err  error
	at   time.Time
}

// Job is one running or finished DAG execution.
type Job struct {
	ID         string
	engine     *Engine
	spec       DagSpec
	nodes      map[string]*NodeSpec
	dependents map[string][]string // dep name -> names depending on it

	mu              sync.Mutex
	states          map[string]*nodeRuntime
	state           JobState
	seq             int
	createdAt       time.Time
	endedAt         time.Time
	cancelRequested bool

	ctx        context.Context
	cancel     context.CancelFunc
	reports    chan nodeReport
	cancelCh   chan struct{}
	cancelOnce sync.Once
	done       chan struct{}
}

func (j *Job) emitLocked(e Event) {
	j.seq++
	e.Seq = j.seq
	if e.Time.IsZero() {
		e.Time = j.engine.clock.Now()
	}
	e.JobID = j.ID
	if e.JobName == "" {
		e.JobName = j.spec.Name
	}
	j.engine.sink.Record(e)
}

// run executes the scheduler loop on its own goroutine.
func (j *Job) run() {
	j.mu.Lock()
	j.emitLocked(Event{Type: EventJobSubmitted, JobName: j.spec.Name})
	j.emitLocked(Event{Type: EventJobStarted})
	// Every node is queued at job start, then dependency-free nodes launch.
	for _, name := range j.sortedNodeNamesLocked() {
		j.emitLocked(Event{Type: EventNodeQueued, Node: name, State: NodePending})
	}
	var ready []string
	for name, rt := range j.states {
		if rt.remaining == 0 {
			ready = append(ready, name)
		}
	}
	for _, name := range ready {
		j.launchLocked(name)
	}
	j.mu.Unlock()

	for {
		// Block until either a node reports or cancellation is requested.
		// There is always something to wait for while the job is unfinished:
		// either an executor/backoff is active, or (under cancellation)
		// pending nodes are swept to terminal immediately.
		j.mu.Lock()
		if j.allTerminalLocked() {
			j.finishLocked()
			j.mu.Unlock()
			return
		}
		active := j.activeCountLocked()
		switch {
		case active > 0:
			// Fall through to blocking wait below.
		case j.cancelRequested:
			// Nothing executing and cancellation seen: all remaining PENDING
			// nodes become terminal skips, so the next iteration finishes.
			j.sweepPendingLocked("job canceled")
			j.mu.Unlock()
			continue
		default:
			// No active work but the graph is incomplete and nobody asked to
			// cancel: cannot happen for a validated graph (every nonterminal
			// node has either a path of pending deps or a running ancestor).
			// Sweep defensively so the job terminates FAILED rather than hangs.
			j.sweepPendingLocked("unreachable: no runnable nodes")
			j.mu.Unlock()
			continue
		}
		j.mu.Unlock()

		select {
		case rep := <-j.reports:
			j.handleReport(rep)
		case <-j.cancelCh:
			j.mu.Lock()
			// Active nodes keep running and will report; only PENDING nodes
			// are swept now. The next loop iteration waits for the actives.
			j.sweepPendingLocked("job canceled")
			j.mu.Unlock()
		}
	}
}

func (j *Job) sortedNodeNamesLocked() []string {
	names := make([]string, 0, len(j.states))
	for name := range j.states {
		names = append(names, name)
	}
	return names
}

// activeCountLocked counts nodes whose completion is reported asynchronously
// (RUNNING executor calls and RETRYING backoff waits).
func (j *Job) activeCountLocked() int {
	n := 0
	for _, rt := range j.states {
		if rt.state == NodeRunning || rt.state == NodeRetrying {
			n++
		}
	}
	return n
}

func (j *Job) allTerminalLocked() bool {
	for _, rt := range j.states {
		if !rt.state.Terminal() {
			return false
		}
	}
	return true
}

func (j *Job) finishLocked() {
	if j.state != JobRunning {
		return
	}
	terminal := j.engine.clock.Now()
	j.endedAt = terminal
	switch {
	case j.cancelRequested:
		// A cancellation request wins: the job is CANCELED even if some
		// in-flight nodes had already exhausted their retries.
		j.state = JobCanceled
		j.emitLocked(Event{Type: EventJobCanceled, JobState: JobCanceled, Time: terminal})
	default:
		failed := false
		for _, rt := range j.states {
			if rt.state == NodeFailed {
				failed = true
				break
			}
		}
		if failed {
			j.state = JobFailed
			j.emitLocked(Event{Type: EventJobFailed, JobState: JobFailed, Time: terminal})
		} else {
			j.state = JobSucceeded
			j.emitLocked(Event{Type: EventJobSucceeded, JobState: JobSucceeded, Time: terminal})
		}
	}
	close(j.done)
}

// launchLocked starts an attempt of a node. The node must be PENDING (first
// attempt) or RETRYING (subsequent). Caller holds j.mu.
func (j *Job) launchLocked(name string) {
	rt := j.states[name]
	rt.attempts++
	rt.state = NodeRunning
	if rt.startedAt.IsZero() {
		rt.startedAt = j.engine.clock.Now()
	}
	j.emitLocked(Event{Type: EventNodeStarted, Node: name, Attempt: rt.attempts, State: NodeRunning})
	spec := rt.spec
	attempt := rt.attempts
	go j.executeAttempt(spec, attempt)
}

// executeAttempt runs one Executor call outside the scheduler lock.
func (j *Job) executeAttempt(spec *NodeSpec, attempt int) {
	ec := &ExecContext{
		JobID: j.ID,
		Node:  spec,
		Ctx:   AsDoneContext(j.ctx),
	}
	err := j.engine.executor.Execute(ec, attempt)
	rep := nodeReport{node: spec.Name, at: j.engine.clock.Now()}
	if err == nil {
		rep.kind = reportFinished
	} else {
		rep.kind = reportFailed
		rep.err = err
	}
	select {
	case j.reports <- rep:
	case <-j.done:
		// Job finalized while blocked sending; the report is irrelevant.
	}
}

// handleReport applies one attempt result and cascades dependency effects.
func (j *Job) handleReport(rep nodeReport) {
	j.mu.Lock()
	defer j.mu.Unlock()

	rt := j.states[rep.node]
	// A report for a node that is no longer RUNNING is stale — ignore it.
	if rt.state != NodeRunning {
		return
	}
	canceled := j.cancelRequested

	if rep.err == nil && !canceled {
		rt.state = NodeSucceeded
		rt.endedAt = rep.at
		j.emitLocked(Event{Type: EventNodeSucceeded, Node: rep.node, Attempt: rt.attempts, State: NodeSucceeded})
		j.cascadeLocked([]string{rep.node})
		return
	}

	if rep.err != nil && rt.attempts < rt.maxAttempts && !canceled {
		// Retry budget remains: wait out the backoff, then run another attempt.
		rt.state = NodeRetrying
		rt.lastErr = rep.err.Error()
		d := j.engine.backoff(rt.attempts)
		j.emitLocked(Event{
			Type:    EventNodeRetry,
			Node:    rep.node,
			Attempt: rt.attempts,
			State:   NodeRetrying,
			Error:   rt.lastErr,
			Backoff: d,
		})
		go j.backoffThenRetry(rt.spec, rt.attempts, d)
		return
	}

	// Terminal: retries exhausted, or success/failure arriving after cancel.
	if rep.err != nil {
		rt.state = NodeFailed
		rt.lastErr = rep.err.Error()
		reason := "retries exhausted"
		if canceled {
			reason = "canceled: " + rep.err.Error()
		}
		j.emitLocked(Event{
			Type:    EventNodeFailed,
			Node:    rep.node,
			Attempt: rt.attempts,
			State:   NodeFailed,
			Error:   rt.lastErr,
			Reason:  reason,
		})
	} else {
		// Finished successfully right as cancel arrived: honor the success.
		rt.state = NodeSucceeded
		j.emitLocked(Event{Type: EventNodeSucceeded, Node: rep.node, Attempt: rt.attempts, State: NodeSucceeded})
	}
	rt.endedAt = rep.at
	j.cascadeLocked([]string{rep.node})
}

// backoffThenRetry waits via the engine Clock and either starts the next
// attempt or, when canceled mid-wait, terminates the node directly under
// the job lock (it must not send to reports, which nobody would read once
// the node is swept).
func (j *Job) backoffThenRetry(spec *NodeSpec, afterAttempt int, d time.Duration) {
	ok := j.engine.clock.Sleep(AsDoneContext(j.ctx), d)
	if ok {
		j.mu.Lock()
		rt := j.states[spec.Name]
		if rt.state == NodeRetrying && rt.attempts == afterAttempt {
			j.launchLocked(spec.Name)
		}
		j.mu.Unlock()
		return
	}
	j.mu.Lock()
	defer j.mu.Unlock()
	rt := j.states[spec.Name]
	if rt.state != NodeRetrying {
		return // raced with another terminal transition
	}
	rt.state = NodeFailed
	rt.endedAt = j.engine.clock.Now()
	msg := "canceled during retry backoff"
	if err := j.ctx.Err(); err != nil {
		msg = msg + ": " + err.Error()
	}
	rt.lastErr = msg
	j.emitLocked(Event{
		Type:    EventNodeFailed,
		Node:    spec.Name,
		Attempt: rt.attempts,
		State:   NodeFailed,
		Error:   msg,
		Reason:  "canceled",
	})
	j.cascadeLocked([]string{spec.Name})
}

// cascadeLocked propagates terminal-state dependencies to their dependents.
// initial holds names of nodes that just became terminal. A dependent is
// launched when all its deps have ended and its policy allows it; otherwise
// it is marked SKIPPED and propagation continues transitively.
// After cancellation nothing new is launched: eligible nodes are skipped.
// Caller holds j.mu.
func (j *Job) cascadeLocked(initial []string) {
	queue := append([]string(nil), initial...)
	for len(queue) > 0 {
		parent := queue[0]
		queue = queue[1:]
		pstate := j.states[parent].state

		for _, child := range j.dependents[parent] {
			rt := j.states[child]
			if rt.state != NodePending {
				continue
			}
			rt.remaining--
			blocked := pstate != NodeSucceeded && rt.policy == RequireAllSuccess
			if blocked {
				j.markSkippedLocked(child, "dependency "+parent+" "+strings.ToLower(string(pstate)))
				queue = append(queue, child)
				continue
			}
			if rt.remaining == 0 {
				if j.cancelRequested {
					j.markSkippedLocked(child, "job canceled before start")
					queue = append(queue, child)
				} else {
					j.launchLocked(child)
				}
			}
		}
	}
}

// markSkippedLocked marks a PENDING node SKIPPED and records the event.
func (j *Job) markSkippedLocked(name, reason string) {
	rt := j.states[name]
	if rt.state.Terminal() {
		return
	}
	rt.state = NodeSkipped
	rt.endedAt = j.engine.clock.Now()
	j.emitLocked(Event{Type: EventNodeSkipped, Node: name, State: NodeSkipped, Reason: reason})
}

// sweepPendingLocked force-skips every PENDING node (used on cancellation),
// then cascades the skips through the graph.
func (j *Job) sweepPendingLocked(reason string) {
	var pending []string
	for name, rt := range j.states {
		if rt.state == NodePending {
			j.markSkippedLocked(name, reason)
			pending = append(pending, name)
		}
	}
	j.cascadeLocked(pending)
}
