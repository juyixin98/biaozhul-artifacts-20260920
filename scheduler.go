package deadlineadm

import (
	"container/heap"
	"context"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"time"

	"deadlineadm/clock"
	"deadlineadm/event"
	"deadlineadm/executor"
)

// Errors returned through the API.
var (
	// ErrInfeasible matches conservative-admission rejections.
	ErrInfeasible = errors.New("admission rejected: deadline predicted infeasible")
	// ErrNotFound is returned for unknown job ids.
	ErrNotFound = errors.New("job not found")
	// ErrTerminal is returned when canceling a job already terminal.
	ErrTerminal = errors.New("job already terminal")
)

// InfeasibleError carries the admission simulation's explanation.
type InfeasibleError struct{ reason string }

func (e *InfeasibleError) Error() string        { return e.reason }
func (e *InfeasibleError) Is(target error) bool { return target == ErrInfeasible }

// Clock is the time/timer source the scheduler needs.
type Clock = clock.Clock

// Request is a job submission.
type Request struct {
	// ID must be unique across all submissions.
	ID string
	// Payload is opaque to the scheduler and handed to the executor.
	Payload string
	// Demand is the number of machine capacity units occupied while running
	// (>=1; defaults to 1).
	Demand int
	// Deadline is the absolute completion deadline.
	Deadline time.Time
	// Budget is the declared upper bound on execution time.
	Budget time.Duration
}

// internal actor messages
type submitReq struct {
	req  Request
	resp chan error
}
type cancelReq struct {
	id   string
	resp chan error
}
type lookupReq struct {
	id   string
	resp chan jobLookup
}
type jobLookup struct {
	view JobView
	ok   bool
}
type listReq struct{ resp chan []JobView }
type statsReq struct{ resp chan Stats }
type eventsReq struct{ resp chan []event.Event }
type resultMsg struct {
	jobID string
	res   executor.Result
}
type syncReq struct{ done chan struct{} }
type stoppedMsg struct{}

// Kill reasons recorded on a running job while it executes.
const (
	killReasonBudget   = "budget_exceeded"
	killReasonDeadline = "deadline_reached"
	killReasonCancel   = "canceled"
)

// Scheduler is a single-machine non-preemptive EDF scheduler with
// conservative admission control. Its mutable state lives on one actor
// goroutine; public methods communicate through channels.
type Scheduler struct {
	cfg     Config
	clk     Clock
	exec    executor.Executor
	rec     *event.Recorder
	memSink *event.MemorySink

	jobs  map[string]*Job
	queue *edfHeap
	seq   int64

	inUse    int
	acquired int
	released int

	rejectedCount int

	submitCh chan submitReq
	cancelCh chan cancelReq
	lookupCh chan lookupReq
	listCh   chan listReq
	statsCh  chan statsReq
	eventsCh chan eventsReq
	resultCh chan resultMsg
	syncCh   chan syncReq
	stopCh   chan stoppedMsg

	wakeTimer clock.Timer // single timer for the earliest internal wake event
	wakeHeap  *wakeHeap
	wakeSeq   int64

	startOnce sync.Once
	stopOnce  sync.Once
	wg        sync.WaitGroup
	inflight  sync.WaitGroup // executor results not yet handed to the actor
	killWG    sync.WaitGroup // blocking wait for kill-result finalization
	// killsOutstanding mirrors killWG's pending count for lock-free polling by
	// test drivers (WaitGroup has no counter accessor). Actor-only writes.
	killsOutstanding atomic.Int64
}

// New creates a scheduler. Call Start to launch its actor. When sink is nil,
// events are retained only in memory; otherwise events go to both the memory
// history and the supplied sink.
func New(cfg Config, clk Clock, exec executor.Executor, sink event.Sink) *Scheduler {
	cfg.withDefaults()
	mem := event.NewMemorySink(cfg.EventCapacity)
	if sink != nil {
		sink = event.Multi(mem, sink)
	} else {
		sink = mem
	}
	h := &edfHeap{}
	heap.Init(h)
	wh := &wakeHeap{}
	heap.Init(wh)
	return &Scheduler{
		cfg:      cfg,
		clk:      clk,
		exec:     exec,
		rec:      event.NewRecorder(clk, sink),
		memSink:  mem,
		jobs:     map[string]*Job{},
		queue:    h,
		wakeHeap: wh,
		submitCh: make(chan submitReq),
		cancelCh: make(chan cancelReq),
		lookupCh: make(chan lookupReq),
		listCh:   make(chan listReq),
		statsCh:  make(chan statsReq),
		eventsCh: make(chan eventsReq),
		resultCh: make(chan resultMsg, 16),
		syncCh:   make(chan syncReq),
		stopCh:   make(chan stoppedMsg),
	}
}

// Start launches the scheduler actor.
func (s *Scheduler) Start() {
	s.startOnce.Do(func() {
		s.wg.Add(1)
		go s.run()
	})
}

// Stop halts the actor and cancels every still-running executor context, so
// no executor goroutine outlives the scheduler. Jobs already terminal are
// unaffected; queued and running jobs are abandoned without further events.
func (s *Scheduler) Stop() {
	s.stopOnce.Do(func() {
		s.stopCh <- stoppedMsg{}
		s.wg.Wait()
	})
}

// Submit applies conservative admission and, if accepted, enqueues the job.
// It returns *InfeasibleError (matched by errors.Is(err, ErrInfeasible)) when
// the job is predicted unable to meet its deadline.
func (s *Scheduler) Submit(req Request) error {
	r := submitReq{req: req, resp: make(chan error, 1)}
	s.submitCh <- r
	return <-r.resp
}

// Cancel cancels a queued or running job.
//
//   - queued: removed immediately; it never held resources, so none are freed.
//   - running: the executor is killed; resources are released exactly once,
//     when the termination result comes back.
//   - terminal: ErrTerminal; unknown id: ErrNotFound.
//
// Repeated cancels cannot double-release: after the first transition the job
// is terminal and later calls get ErrTerminal.
func (s *Scheduler) Cancel(id string) error {
	r := cancelReq{id: id, resp: make(chan error, 1)}
	s.cancelCh <- r
	return <-r.resp
}

// Get fetches one job snapshot.
func (s *Scheduler) Get(id string) (JobView, bool) {
	r := lookupReq{id: id, resp: make(chan jobLookup, 1)}
	s.lookupCh <- r
	v := <-r.resp
	return v.view, v.ok
}

// List returns snapshots of all jobs in submission order.
func (s *Scheduler) List() []JobView {
	r := listReq{resp: make(chan []JobView, 1)}
	s.listCh <- r
	return <-r.resp
}

// Stats returns counters and the resource-conservation invariant.
func (s *Scheduler) Stats() Stats {
	r := statsReq{resp: make(chan Stats, 1)}
	s.statsCh <- r
	return <-r.resp
}

// Events returns the retained structured event log.
func (s *Scheduler) Events() []event.Event {
	r := eventsReq{resp: make(chan []event.Event, 1)}
	s.eventsCh <- r
	return <-r.resp
}

// Now returns the scheduler clock's current time. Safe without actor
// synchronization because clocks are independently goroutine-safe.
func (s *Scheduler) Now() time.Time { return s.clk.Now() }

// sync blocks until all earlier actor messages have been processed. Used by
// the fake-clock test driver to drain cascades between firings.
func (s *Scheduler) sync() {
	done := make(chan struct{})
	s.syncCh <- syncReq{done: done}
	<-done
}

// ---- actor ----

func (s *Scheduler) run() {
	defer s.wg.Done()
	s.rebuildWake()
	for {
		select {
		case r := <-s.submitCh:
			r.resp <- s.handleSubmit(r.req)
		case r := <-s.cancelCh:
			r.resp <- s.handleCancel(r.id)
		case r := <-s.lookupCh:
			if j, ok := s.jobs[r.id]; ok {
				r.resp <- jobLookup{view: s.view(j), ok: true}
			} else {
				r.resp <- jobLookup{}
			}
		case r := <-s.listCh:
			r.resp <- s.handleList()
		case r := <-s.statsCh:
			r.resp <- s.handleStats()
		case r := <-s.eventsCh:
			r.resp <- s.memSink.Events()
		case m := <-s.resultCh:
			s.handleResult(m.jobID, m.res)
		case <-s.wakeChan():
			s.wakeTimer = nil
			s.handleWakes()
		case r := <-s.syncCh:
			close(r.done)
		case <-s.stopCh:
			s.disarmWake()
			s.abortAll()
			return
		}
		s.rebuildWake()
	}
}

// abortAll cancels the executor context of every running job and clears the
// queues on shutdown. Terminal results that arrive afterwards are ignored by
// the now-stopped actor; resultCh is buffered so forwarding goroutines never
// block.
func (s *Scheduler) abortAll() {
	for _, j := range s.jobs {
		if j.status == StatusRunning && j.stopDeadline != nil {
			j.stopDeadline()
		}
	}
	h := &edfHeap{}
	heap.Init(h)
	wh := &wakeHeap{}
	heap.Init(wh)
	s.queue = h
	s.wakeHeap = wh
}

func (s *Scheduler) wakeChan() <-chan time.Time {
	if s.wakeTimer == nil {
		return nil
	}
	return s.wakeTimer.C()
}

// schedulerTimerClock is implemented by the fake clock so the scheduler's
// internal wake timer is kept out of the event stream that deterministic test
// drivers advance through (executor sleeps are the driver-visible events).
type schedulerTimerClock interface {
	NewSchedulerTimer(d time.Duration) clock.Timer
}

// rebuildWake (re)installs the single internal timer to match the earliest
// event in the wake heap. It runs after every actor message, so stale timers
// never survive a state change.
func (s *Scheduler) rebuildWake() {
	s.disarmWake()
	if s.wakeHeap.Len() == 0 {
		return
	}
	at := (*s.wakeHeap)[0].at
	d := at.Sub(s.clk.Now())
	if d < 0 {
		d = 0
	}
	if mk, ok := s.clk.(schedulerTimerClock); ok {
		s.wakeTimer = mk.NewSchedulerTimer(d)
	} else {
		s.wakeTimer = s.clk.NewTimer(d)
	}
}

func (s *Scheduler) disarmWake() {
	if s.wakeTimer != nil {
		s.wakeTimer.Stop()
		s.wakeTimer = nil
	}
}

// addWake registers one internal timed event.
func (s *Scheduler) addWake(at time.Time, kind wakeKind, jobID string) {
	s.wakeSeq++
	heap.Push(s.wakeHeap, &wakeEvent{at: at, kind: kind, jobID: jobID, seq: s.wakeSeq})
}

// replaceWake removes every pending wake for jobID and adds one new event.
func (s *Scheduler) replaceWake(jobID string, at time.Time, kind wakeKind) {
	s.removeJobWakes(jobID)
	s.addWake(at, kind, jobID)
}

// removeJobWakes drops all pending wake events for a job (used when it reaches
// a terminal state by another path, e.g. natural completion before its bound).
func (s *Scheduler) removeJobWakes(jobID string) {
	kept := make([]*wakeEvent, 0, s.wakeHeap.Len())
	for s.wakeHeap.Len() > 0 {
		e := heap.Pop(s.wakeHeap).(*wakeEvent)
		if e.jobID != jobID {
			kept = append(kept, e)
		}
	}
	for _, e := range kept {
		heap.Push(s.wakeHeap, e)
	}
}

// handleWakes processes every internal event due by the current time.
func (s *Scheduler) handleWakes() {
	now := s.clk.Now()
	for s.wakeHeap.Len() > 0 && !(*s.wakeHeap)[0].at.After(now) {
		ev := heap.Pop(s.wakeHeap).(*wakeEvent)
		j, ok := s.jobs[ev.jobID]
		if !ok || j.status.Terminal() {
			continue // stale: job already gone
		}
		switch ev.kind {
		case wakeQueuedDeadline:
			if j.status == StatusQueued {
				heap.Remove(s.queue, j.heapIndex)
				s.finalize(j, StatusDeadlineMissed, "", "deadline passed while queued", nil)
			}
		case wakeRunningBound:
			if j.status == StatusRunning {
				// Bound equals the deadline -> deadline miss; otherwise the
				// kill happened at the declared budget -> runtime timeout.
				s.beginKill(j)
				if j.boundAt.Equal(j.deadline) {
					j.kill(killReasonDeadline)
				} else {
					j.kill(killReasonBudget)
				}
			}
		}
	}
}

func (s *Scheduler) handleSubmit(req Request) error {
	now := s.clk.Now()

	if req.ID == "" {
		return errors.New("missing job id")
	}
	if _, exists := s.jobs[req.ID]; exists {
		return fmt.Errorf("duplicate job id %q", req.ID)
	}
	if req.Budget <= 0 {
		return errors.New("budget must be positive")
	}
	if req.Demand < 0 {
		return errors.New("demand must not be negative")
	}
	if req.Demand == 0 {
		req.Demand = 1
	}

	reject := func(reason string, detail map[string]any) error {
		s.rejectedCount++
		s.rec.Emit(event.Submitted, req.ID, "rejected", detail)
		s.rec.Emit(event.Rejected, req.ID, reason, detail)
		return &InfeasibleError{reason: reason}
	}

	if req.Demand > s.cfg.Capacity {
		return reject(
			fmt.Sprintf("job %q demands %d units but machine capacity is %d", req.ID, req.Demand, s.cfg.Capacity),
			map[string]any{"demand": req.Demand, "capacity": s.cfg.Capacity})
	}
	if !req.Deadline.After(now) {
		return reject(
			fmt.Sprintf("job %q deadline %d ms is not in the future (now %d ms)", req.ID, req.Deadline.UnixMilli(), now.UnixMilli()),
			map[string]any{"deadline_ms": req.Deadline.UnixMilli(), "now_ms": now.UnixMilli()})
	}
	if req.Deadline.Sub(now) < req.Budget {
		return reject(
			fmt.Sprintf("job %q cannot meet deadline %d ms even if started now: budget %d ms > time to deadline %d ms",
				req.ID, req.Deadline.UnixMilli(), req.Budget.Milliseconds(), req.Deadline.Sub(now).Milliseconds()),
			map[string]any{"budget_ms": req.Budget.Milliseconds(), "available_ms": req.Deadline.Sub(now).Milliseconds()})
	}

	items := s.admissionItems(now, req)
	res := SimulateEDF(now, s.cfg.Capacity, items)
	if !res.Feasible {
		return reject(res.Reason, map[string]any{"missed_on": res.MissedOn})
	}

	s.seq++
	j := &Job{
		seq:       s.seq,
		id:        req.ID,
		payload:   req.Payload,
		demand:    req.Demand,
		releaseAt: now,
		deadline:  req.Deadline,
		budget:    req.Budget,
		status:    StatusQueued,
		heapIndex: -1,
	}
	s.jobs[j.id] = j
	heap.Push(s.queue, j)
	s.addWake(j.deadline, wakeQueuedDeadline, j.id)
	s.rec.Emit(event.Submitted, j.id, "admitted", map[string]any{
		"demand": j.demand, "deadline_ms": j.deadline.UnixMilli(), "budget_ms": j.budget.Milliseconds(),
	})
	s.rec.Emit(event.Admitted, j.id, "", nil)
	s.pump(now)
	return nil
}

// admissionItems assembles the conservative test set: candidate plus every
// unfinished accepted job, with running jobs' residual adjusted for overrun
// policy.
func (s *Scheduler) admissionItems(now time.Time, candidate Request) []WorkItem {
	items := make([]WorkItem, 0, len(s.jobs)+1)
	for _, j := range s.jobs {
		if j.status.Terminal() {
			continue
		}
		if j.status == StatusQueued {
			items = append(items, WorkItem{
				Tag: j.id, Demand: j.demand, Release: j.releaseAt,
				Deadline: j.deadline, Remaining: j.budget,
			})
			continue
		}
		// running
		elapsed := now.Sub(time.UnixMilli(j.startMs))
		rem := j.budget - elapsed
		if rem < 0 {
			rem = 0
		}
		if s.cfg.Overrun == PolicyObserve {
			if bound := j.deadline.Sub(now); bound < rem {
				rem = bound
			}
		}
		items = append(items, WorkItem{
			Tag: j.id, Demand: j.demand, Release: now,
			Deadline: j.deadline, Remaining: rem,
		})
	}
	items = append(items, WorkItem{
		Tag: candidate.ID, Demand: candidate.Demand, Release: now,
		Deadline: candidate.Deadline, Remaining: candidate.Budget,
	})
	return items
}

// pump dispatches queued EDF jobs while capacity permits, head-blocks-queue.
func (s *Scheduler) pump(now time.Time) {
	for s.queue.Len() > 0 {
		head := (*s.queue)[0]
		if s.inUse+head.demand > s.cfg.Capacity {
			break
		}
		heap.Pop(s.queue)
		s.startJob(head, now)
	}
}

// startJob acquires resources and launches execution. Resources are released
// only in finalize(), which runs exactly once per job (actor-serialized and
// guarded by j.released).
//
// The executor context is purely manual: it is canceled either by the
// scheduler's internal bound timer (budget/deadline kill) or by an explicit
// cancel. Natural completion (the executor's own sleep timer) therefore never
// races an ambient clock deadline: when a job finishes on time the bound timer
// is disarmed before it can fire.
func (s *Scheduler) startJob(j *Job, now time.Time) {
	s.inUse += j.demand
	s.acquired += j.demand
	j.status = StatusRunning
	j.startMs = now.UnixMilli()
	j.started = true

	// Absolute instant at which the scheduler kills the job.
	bound := j.deadline
	if s.cfg.Overrun == PolicyKillAtBudget {
		if be := now.Add(j.budget); be.Before(bound) {
			bound = be
		}
	}
	j.boundAt = bound
	// The queued-deadline wake is superseded by the running bound wake.
	s.replaceWake(j.id, bound, wakeRunningBound)

	ctx, stop := context.WithCancelCause(context.Background())
	jobID := j.id
	ch := s.exec.Start(ctx, j.id, j.payload)

	j.stopDeadline = func() { stop(nil) }
	j.kill = func(reason string) {
		j.killReason = reason
		// Budget/deadline kills surface as DeadlineExceeded so the executor
		// reports ErrKilled; explicit cancel surfaces as Canceled.
		if reason == killReasonCancel {
			stop(context.Canceled)
		} else {
			stop(context.DeadlineExceeded)
		}
	}
	j.killReason = ""

	s.rec.Emit(event.Started, j.id, "", map[string]any{"demand": j.demand, "start_ms": j.startMs})

	// Count the in-flight result before the goroutine can finish, so a test
	// driver never reaches Wait() before Add (WaitGroup forbids that).
	s.inflight.Add(1)
	go func() {
		defer s.inflight.Done()
		r, ok := <-ch
		if !ok {
			return
		}
		// resultCh is buffered, so enqueueing never blocks on the actor.
		s.resultCh <- resultMsg{jobID: jobID, res: r}
	}()
}

// beginKill records that a running job was killed and its executor result is
// pending. Actor-only.
func (s *Scheduler) beginKill(j *Job) {
	s.killWG.Add(1)
	s.killsOutstanding.Add(1)
	j.killPending = true
}

// endKill matches one beginKill after the kill result is finalized.
func (s *Scheduler) endKill() {
	s.killWG.Done()
	s.killsOutstanding.Add(-1)
}

// waitInflight blocks until every executor result has been handed to the
// actor mailbox. Tests drive fake clocks and need this to make completion
// cascades deterministic; production code never calls it.
func (s *Scheduler) waitInflight() { s.inflight.Wait() }

// waitKills blocks until every killed running job has had its executor result
// finalized. Used by the fake-clock test driver.
func (s *Scheduler) waitKills() { s.killWG.Wait() }

// WaitKills blocks until running jobs canceled or bound-killed synchronously
// (outside a clock advance) have had their termination finalized. It is used
// by deterministic fake-clock drivers and demos after a direct Cancel of a
// running job.
func (s *Scheduler) WaitKills() { s.killWG.Wait() }

// killsPending reports without touching job fields whether a kill result is
// still outstanding (safe to call from a test goroutine after an actor sync).
func (s *Scheduler) killsPending() bool { return s.killsOutstanding.Load() > 0 }

func (s *Scheduler) handleCancel(id string) error {
	j, ok := s.jobs[id]
	if !ok {
		return ErrNotFound
	}
	switch j.status {
	case StatusQueued:
		heap.Remove(s.queue, j.heapIndex)
		s.finalize(j, StatusCanceled, killReasonCancel, "", nil)
		return nil
	case StatusRunning:
		// The kill is asynchronous: the executor must confirm termination
		// before resources are released. Track it so test drivers can wait for
		// finalization; handleResult does the matching endKill.
		s.beginKill(j)
		j.kill(killReasonCancel)
		return nil
	default:
		return ErrTerminal
	}
}

// handleQueueDeadlines finalizes every queued job whose deadline has arrived
// while it waited. Running jobs are not touched here; their executor context
// bounds them independently.
// handleResult classifies an executor termination and finalizes exactly once.
func (s *Scheduler) handleResult(id string, r executor.Result) {
	j, ok := s.jobs[id]
	if !ok {
		// Should never happen; an unknown result cannot touch the ledger.
		return
	}
	if j.status != StatusRunning {
		return // already finalized (defensive: no double release)
	}

	killPending := j.killPending
	defer func() {
		if killPending {
			s.endKill()
		}
	}()

	now := s.clk.Now()
	j.ranMs = r.Ran.Milliseconds()

	switch {
	case r.Err == nil:
		// Natural completion.
		if now.After(j.deadline) {
			// Only reachable in observe mode: the job was allowed to run past
			// its declared bound but not past the deadline safety kill.
			s.finalize(j, StatusDeadlineMissed, "", "completed after deadline", r.Err)
		} else {
			s.finalize(j, StatusCompleted, "", "", nil)
		}
	case errors.Is(r.Err, executor.ErrKilled):
		switch j.killReason {
		case killReasonBudget:
			s.finalize(j, StatusTimeout, killReasonBudget,
				fmt.Sprintf("execution exceeded declared upper bound of %d ms", j.budget.Milliseconds()), r.Err)
		case killReasonDeadline:
			s.finalize(j, StatusDeadlineMissed, killReasonDeadline,
				"still running at deadline", r.Err)
		default:
			// Deadline context fired without an actor-recorded reason.
			if now.After(j.deadline) || now.Equal(j.deadline) {
				s.finalize(j, StatusDeadlineMissed, killReasonDeadline, "still running at deadline", r.Err)
			} else {
				s.finalize(j, StatusTimeout, killReasonBudget, "killed at declared bound", r.Err)
			}
		}
	case errors.Is(r.Err, executor.ErrCanceled) || j.killReason == killReasonCancel:
		s.finalize(j, StatusCanceled, killReasonCancel, "", r.Err)
	default:
		s.finalize(j, StatusFailed, "", "executor reported an error", r.Err)
	}
}

// finalize performs the single terminal transition and the single resource
// release for a job. All calls run on the actor; the released guard makes the
// invariant explicit and enforced.
func (s *Scheduler) finalize(j *Job, status Status, reason, msg string, execErr error) {
	if j.status.Terminal() {
		return
	}
	// Any pending internal wake for this job is obsolete once it is terminal.
	s.removeJobWakes(j.id)
	now := s.clk.Now()
	j.status = status
	j.reason = msg
	j.endMs = now.UnixMilli()

	detail := map[string]any{
		"end_ms":      j.endMs,
		"deadline_ms": j.deadline.UnixMilli(),
		"ran_ms":      j.ranMs,
	}
	if reason != "" {
		detail["reason"] = reason
	}
	if execErr != nil {
		detail["executor_error"] = execErr.Error()
	}

	var et event.Type
	switch status {
	case StatusCompleted:
		et = event.Completed
	case StatusDeadlineMissed:
		et = event.DeadlineMissed
	case StatusTimeout:
		et = event.Timeout
	case StatusCanceled:
		et = event.Canceled
	case StatusFailed:
		et = event.Failed
	}

	// Resource release: exactly once, only for jobs that actually started.
	if j.started {
		if j.released {
			panic(fmt.Sprintf("deadlineadm: double resource release for job %q", j.id))
		}
		s.inUse -= j.demand
		s.released += j.demand
		j.released = true
		detail["released_demand"] = j.demand
		if j.stopDeadline != nil {
			j.stopDeadline()
		}
	}
	s.rec.Emit(et, j.id, reason, detail)

	// A terminal transition may free capacity for queued jobs.
	s.pump(now)
}

func (s *Scheduler) handleList() []JobView {
	out := make([]*Job, 0, len(s.jobs))
	for _, j := range s.jobs {
		out = append(out, j)
	}
	sortBySeq(out)
	views := make([]JobView, len(out))
	for i, j := range out {
		views[i] = s.view(j)
	}
	return views
}

func (s *Scheduler) handleStats() Stats {
	now := s.clk.Now()
	st := Stats{
		NowMs:         now.UnixMilli(),
		Capacity:      s.cfg.Capacity,
		InUse:         s.inUse,
		Queued:        s.queue.Len(),
		AcquiredTotal: s.acquired,
		ReleasedTotal: s.released,
	}
	running := 0
	for _, j := range s.jobs {
		switch j.status {
		case StatusRunning:
			running++
		case StatusCompleted:
			st.Completed++
		case StatusDeadlineMissed:
			st.DeadlineMissed++
		case StatusTimeout:
			st.Timeout++
		case StatusCanceled:
			st.Canceled++
		case StatusFailed:
			st.Failed++
		}
		if j.status.Terminal() {
			switch j.status {
			case StatusCompleted:
				st.MetDeadline++
			case StatusDeadlineMissed:
				// Deadline statistics track jobs that ran (or waited) past
				// their deadline. Timeouts are a distinct kind (killed for
				// exceeding the declared bound); canceled and failed jobs are
				// excluded from deadline accounting altogether.
				st.MissedDeadline++
			}
		}
	}
	st.Running = running
	st.Submitted = len(s.jobs)
	st.Rejected = s.rejectedCount
	st.Conserved = s.acquired == s.released+s.inUse
	return st
}

func sortBySeq(jobs []*Job) {
	// simple insertion sort keeps FIFO order without extra imports churn
	for i := 1; i < len(jobs); i++ {
		for k := i; k > 0 && jobs[k-1].seq > jobs[k].seq; k-- {
			jobs[k-1], jobs[k] = jobs[k], jobs[k-1]
		}
	}
}
