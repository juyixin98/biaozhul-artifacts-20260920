package scheduler

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"sync"
	"time"
)

// Executor runs one job to completion. It must honour ctx cancellation and
// return promptly when ctx is done. The executor is replaceable: production
// code can run real work, tests can simulate durations on a ManualClock.
type Executor interface {
	Run(ctx context.Context, j *Job) error
}

// ExecutorFunc adapts a plain function to Executor.
type ExecutorFunc func(ctx context.Context, j *Job) error

func (f ExecutorFunc) Run(ctx context.Context, j *Job) error { return f(ctx, j) }

// SleepExecutor is a demo executor: it "works" by waiting SimActual (or the
// declared bound when SimActual is unset) on the given clock.
type SleepExecutor struct{ Clock Clock }

func (e SleepExecutor) Run(ctx context.Context, j *Job) error {
	d := j.SimActual
	if d <= 0 {
		d = j.ExecBound
	}
	select {
	case <-e.Clock.After(d):
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

var (
	ErrNotFound       = errors.New("job not found")
	ErrNotCancellable = errors.New("job is not in a cancellable state")
	ErrInvalidSpec    = errors.New("invalid job spec")
	ErrInfeasible     = errors.New("deadline cannot be guaranteed (conservative admission)")
)

// Stats are cumulative counters plus the current slot occupancy. Slot
// conservation requires Acquired == Released whenever nothing is running.
type Stats struct {
	Submitted          int  `json:"submitted"`
	Admitted           int  `json:"admitted"`
	RejectedInfeasible int  `json:"rejected_infeasible"`
	Completed          int  `json:"completed"`
	TimedOut           int  `json:"timed_out"`
	Cancelled          int  `json:"cancelled"`
	DeadlineMet        int  `json:"deadline_met"`
	DeadlineMissed     int  `json:"deadline_missed"`
	SlotAcquired       int  `json:"slot_acquired"`
	SlotReleased       int  `json:"slot_released"`
	SlotInUse          bool `json:"slot_in_use"`
}

// Scheduler is a single-machine, non-preemptive EDF scheduler with
// conservative admission control and one execution slot.
type Scheduler struct {
	clock Clock
	exec  Executor
	log   *EventLog

	mu        sync.Mutex
	queue     []*Job // kept in EDF order (earliest deadline first)
	running   *Job
	finished  []*Job // terminal jobs, kept for inspection
	cancelRun context.CancelFunc
	wake      chan struct{}
	stop      chan struct{}
	stopped   chan struct{}
	stats     Stats
	nextID    int
}

func New(clock Clock, exec Executor, log *EventLog) *Scheduler {
	return &Scheduler{
		clock: clock,
		exec:  exec,
		log:   log,
		wake:  make(chan struct{}, 1),
		stop:  make(chan struct{}),
	}
}

// Log returns the scheduler's event log.
func (s *Scheduler) Log() *EventLog { return s.log }

// Now returns the scheduler clock's current time.
func (s *Scheduler) Now() time.Time { return s.clock.Now() }

// Submit validates a spec, runs conservative admission control, and either
// enqueues the job (returning it in StateQueued) or rejects it as infeasible.
func (s *Scheduler) Submit(spec JobSpec) (*Job, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	s.stats.Submitted++
	if spec.ExecBound <= 0 {
		return nil, fmt.Errorf("%w: exec bound must be positive", ErrInvalidSpec)
	}
	now := s.clock.Now()
	if !spec.Deadline.After(now) {
		return nil, fmt.Errorf("%w: deadline must be in the future", ErrInvalidSpec)
	}
	if spec.ID == "" {
		s.nextID++
		spec.ID = fmt.Sprintf("job-%d", s.nextID)
	} else if s.findLocked(spec.ID) != nil {
		return nil, fmt.Errorf("%w: duplicate id %q", ErrInvalidSpec, spec.ID)
	}

	j := &Job{JobSpec: spec, SubmittedAt: now, State: StateQueued}
	s.log.Record(j.ID, EventSubmitted, map[string]string{
		"exec_bound": spec.ExecBound.String(),
		"deadline":   spec.Deadline.Format(time.RFC3339Nano),
	})

	if !s.feasibleLocked(j, now) {
		j.State = StateRejectedInfeasible
		s.stats.RejectedInfeasible++
		s.finished = append(s.finished, j)
		s.log.Record(j.ID, EventRejectedInfeasible, map[string]string{
			"reason": "edf simulation misses a deadline",
		})
		cp := *j
		return &cp, ErrInfeasible
	}

	s.insertEDFLocked(j)
	s.stats.Admitted++
	s.log.Record(j.ID, EventAdmitted, nil)
	s.signalWake()
	cp := *j
	return &cp, nil
}

// feasibleLocked is the conservative admission test for a non-preemptive
// single machine: simulate EDF execution of the queued jobs plus the
// candidate, starting only after the running job's remaining declared bound
// (the running job cannot be preempted). Every simulated completion must
// meet its deadline.
func (s *Scheduler) feasibleLocked(candidate *Job, now time.Time) bool {
	start := now
	if s.running != nil {
		if rem := s.running.ExecBound - now.Sub(s.running.StartedAt); rem > 0 {
			start = now.Add(rem)
		}
	}
	jobs := make([]*Job, 0, len(s.queue)+1)
	jobs = append(jobs, s.queue...)
	jobs = append(jobs, candidate)
	sort.SliceStable(jobs, func(a, b int) bool {
		return jobs[a].Deadline.Before(jobs[b].Deadline)
	})
	t := start
	for _, j := range jobs {
		t = t.Add(j.ExecBound)
		if t.After(j.Deadline) {
			return false
		}
	}
	return true
}

// insertEDFLocked inserts j keeping the queue ordered by deadline; ties keep
// submission order (FIFO among equal deadlines).
func (s *Scheduler) insertEDFLocked(j *Job) {
	i := sort.Search(len(s.queue), func(i int) bool {
		return s.queue[i].Deadline.After(j.Deadline)
	})
	s.queue = append(s.queue, nil)
	copy(s.queue[i+1:], s.queue[i:])
	s.queue[i] = j
}

// Cancel cancels a queued or running job. Cancellation never touches the
// slot directly: a queued job holds no resource, and a running job's slot is
// released exactly once by the worker when the executor returns.
func (s *Scheduler) Cancel(id string) error {
	s.mu.Lock()
	j := s.findLocked(id)
	if j == nil {
		s.mu.Unlock()
		return ErrNotFound
	}
	switch j.State {
	case StateQueued:
		s.removeFromQueueLocked(j)
		j.State = StateCancelled
		j.FinishedAt = s.clock.Now()
		s.stats.Cancelled++
		s.finished = append(s.finished, j)
		s.mu.Unlock()
		s.log.Record(id, EventCancelled, map[string]string{"phase": "queued"})
		return nil
	case StateRunning:
		if j.cancelRequested {
			s.mu.Unlock()
			return ErrNotCancellable
		}
		j.cancelRequested = true
		cancel := s.cancelRun
		s.mu.Unlock()
		s.log.Record(id, EventCancelRequested, map[string]string{"phase": "running"})
		if cancel != nil {
			cancel()
		}
		return nil
	default:
		s.mu.Unlock()
		return ErrNotCancellable
	}
}

func (s *Scheduler) findLocked(id string) *Job {
	if s.running != nil && s.running.ID == id {
		return s.running
	}
	for _, j := range s.queue {
		if j.ID == id {
			return j
		}
	}
	for _, j := range s.finished {
		if j.ID == id {
			return j
		}
	}
	return nil
}

func (s *Scheduler) removeFromQueueLocked(j *Job) {
	for i, q := range s.queue {
		if q == j {
			s.queue = append(s.queue[:i], s.queue[i+1:]...)
			return
		}
	}
}

func (s *Scheduler) signalWake() {
	select {
	case s.wake <- struct{}{}:
	default:
	}
}

// Start launches the worker goroutine. It returns a channel closed when the
// worker has fully stopped after Stop is called.
func (s *Scheduler) Start() <-chan struct{} {
	s.stopped = make(chan struct{})
	go s.worker()
	return s.stopped
}

// Stop asks the worker to exit after the current job (if any) finishes.
func (s *Scheduler) Stop() { close(s.stop) }

func (s *Scheduler) worker() {
	defer close(s.stopped)
	for {
		select {
		case <-s.stop:
			return
		case <-s.wake:
		}
		for {
			j := s.dequeueNext()
			if j == nil {
				break
			}
			s.runJob(j)
		}
	}
}

func (s *Scheduler) dequeueNext() *Job {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.queue) == 0 || s.running != nil {
		return nil
	}
	j := s.queue[0]
	s.queue = s.queue[1:]
	s.running = j
	return j
}

// runJob executes one job non-preemptively, enforcing the declared bound as
// a hard timeout, and releases the execution slot exactly once on exit.
func (s *Scheduler) runJob(j *Job) {
	s.mu.Lock()
	j.State = StateRunning
	j.StartedAt = s.clock.Now()
	s.stats.SlotAcquired++
	s.stats.SlotInUse = true
	ctx, cancel := context.WithCancel(context.Background())
	s.cancelRun = cancel
	// Register the hard timeout while still holding the lock, so that any
	// observer who can see State==Running is guaranteed the timer exists.
	timeout := s.clock.After(j.ExecBound)
	s.mu.Unlock()

	s.log.Record(j.ID, EventSlotAcquired, nil)
	s.log.Record(j.ID, EventStarted, nil)

	done := make(chan error, 1)
	go func() { done <- s.exec.Run(ctx, j) }()

	var outcome State
	select {
	case <-done:
		s.mu.Lock()
		if j.cancelRequested {
			outcome = StateCancelled
		} else {
			outcome = StateComplete
		}
		s.mu.Unlock()
	case <-timeout:
		// Declared bound exceeded: stop the executor and wait for it.
		cancel()
		<-done
		outcome = StateTimeout
	}
	cancel()

	finished := s.clock.Now()
	s.mu.Lock()
	j.State = outcome
	j.FinishedAt = finished
	s.running = nil
	s.cancelRun = nil
	s.finished = append(s.finished, j)
	s.stats.SlotReleased++
	s.stats.SlotInUse = false
	switch outcome {
	case StateComplete:
		s.stats.Completed++
	case StateTimeout:
		s.stats.TimedOut++
	case StateCancelled:
		s.stats.Cancelled++
	}
	met, hasVerdict := j.DeadlineMet()
	if hasVerdict {
		if met {
			s.stats.DeadlineMet++
		} else {
			s.stats.DeadlineMissed++
		}
	}
	s.mu.Unlock()

	s.log.Record(j.ID, EventSlotReleased, nil)
	switch outcome {
	case StateComplete:
		s.log.Record(j.ID, EventCompleted, map[string]string{"finished_at": finished.Format(time.RFC3339Nano)})
	case StateTimeout:
		s.log.Record(j.ID, EventTimeout, map[string]string{
			"declared_bound": j.ExecBound.String(),
			"stopped_at":     finished.Format(time.RFC3339Nano),
		})
	case StateCancelled:
		s.log.Record(j.ID, EventCancelled, map[string]string{"phase": "running"})
	}
	if hasVerdict {
		if met {
			s.log.Record(j.ID, EventDeadlineMet, nil)
		} else {
			s.log.Record(j.ID, EventDeadlineMissed, map[string]string{
				"deadline": j.Deadline.Format(time.RFC3339Nano),
			})
		}
	}
}

// Get returns a snapshot of a job by ID, searching queued, running and
// finished jobs.
func (s *Scheduler) Get(id string) (*Job, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if j := s.findLocked(id); j != nil {
		cp := *j
		return &cp, true
	}
	return nil, false
}

// List returns snapshots of all known jobs: running, queued, then finished.
func (s *Scheduler) List() []*Job {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []*Job
	if s.running != nil {
		cp := *s.running
		out = append(out, &cp)
	}
	for _, j := range s.queue {
		cp := *j
		out = append(out, &cp)
	}
	for _, j := range s.finished {
		cp := *j
		out = append(out, &cp)
	}
	return out
}

// Stats returns a snapshot of the scheduler counters.
func (s *Scheduler) Stats() Stats {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.stats
}
