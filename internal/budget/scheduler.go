package budget

import (
	"errors"
	"fmt"
	"sync"

	"tokenbudget/internal/clock"
	"tokenbudget/internal/event"
)

// JobFunc is the work executed once the two-layer reservation is granted.
type JobFunc func() error

// JobStatus enumerates scheduler job states.
type JobStatus string

const (
	JobQueued    JobStatus = "queued"
	JobRunning   JobStatus = "running"
	JobSucceeded JobStatus = "succeeded"
	JobFailed    JobStatus = "failed"
	JobRejected  JobStatus = "rejected"
)

// Job is the recorded state of one scheduled request.
type Job struct {
	ID      string        `json:"id"`
	Tenant  string        `json:"tenant"`
	Tokens  int64         `json:"tokens"`
	Name    string        `json:"name"`
	Status  JobStatus     `json:"status"`
	ReadyAt clock.Instant `json:"ready_at_ns"`
	WaitNS  int64         `json:"wait_ns"`
	Err     string        `json:"error,omitempty"`
}

// Handle is a live handle returned at submission time.
type Handle struct {
	rec *jobRec
	ch  chan struct{}
}

// Done returns a channel closed when the job reaches a terminal state.
func (h *Handle) Done() <-chan struct{} { return h.ch }

// Result returns the final Job; only valid after Done() fires.
func (h *Handle) Result() Job {
	h.rec.mu.Lock()
	defer h.rec.mu.Unlock()
	return h.rec.Job
}

type jobRec struct {
	mu sync.Mutex
	Job
}

// Scheduler accepts requests that need tokens "now or later": it reserves a
// two-layer budget and, via the injected clock.Scheduler, runs the work at the
// earliest instant both buckets can afford it. Tokens are consumed at
// reservation time (firm reservation semantics); cancellation does not refund.
type Scheduler struct {
	limiter *Limiter
	sched   clock.Scheduler
	bus     event.Bus
	horizon int64

	mu     sync.Mutex
	nextID uint64
	jobs   map[string]*jobRec
}

// NewScheduler wires a limiter to a delayed executor. horizonNS bounds how far
// in the future a job may be scheduled (<= 0 disables the bound).
func NewScheduler(l *Limiter, s clock.Scheduler, bus event.Bus, horizonNS int64) *Scheduler {
	return &Scheduler{limiter: l, sched: s, bus: bus, horizon: horizonNS, jobs: make(map[string]*jobRec)}
}

// ErrRejected marks a request that could not even be scheduled.
var ErrRejected = errors.New("budget: request rejected")

// Submit reserves tokens and schedules fn. On rejection nothing is consumed
// and a job_queued-less job_rejected event is emitted by the limiter's deny
// path plus a scheduler-level rejection event.
func (s *Scheduler) Submit(tenant, name string, tokens int64, fn JobFunc) (*Handle, error) {
	if fn == nil {
		fn = func() error { return nil }
	}
	readyAt, wait, _, err := s.limiter.Reserve(tenant, tokens, s.horizon)
	if err != nil {
		s.mu.Lock()
		s.nextID++
		id := fmt.Sprintf("job-%d", s.nextID)
		rec := &jobRec{Job: Job{ID: id, Tenant: tenant, Name: name, Tokens: tokens,
			Status: JobRejected}}
		s.jobs[id] = rec
		s.mu.Unlock()
		if s.bus != nil {
			reason := ReasonInsufficient
			if errors.Is(err, ErrTokensExceedCap) {
				reason = ReasonExceedsCap
			}
			s.bus.Emit(event.KindJobRejected, event.Detail{
				Scope: event.ScopeSystem, Tenant: tenant, JobID: id,
				Name: name, Tokens: tokens, Reason: reason,
			})
		}
		h := &Handle{rec: rec, ch: make(chan struct{})}
		close(h.ch)
		return h, fmt.Errorf("%w: %w", ErrRejected, err)
	}

	s.mu.Lock()
	s.nextID++
	id := fmt.Sprintf("job-%d", s.nextID)
	rec := &jobRec{Job: Job{ID: id, Tenant: tenant, Name: name, Tokens: tokens,
		Status: JobQueued, ReadyAt: readyAt, WaitNS: wait}}
	s.jobs[id] = rec
	s.mu.Unlock()
	h := &Handle{rec: rec, ch: make(chan struct{})}

	if s.bus != nil {
		s.bus.Emit(event.KindJobQueued, event.Detail{
			Scope: event.ScopeSystem, Tenant: tenant, JobID: id, Name: name,
			Tokens: tokens, WaitNS: wait,
		})
	}

	s.sched.After(readyAt, func() {
		rec.mu.Lock()
		rec.Status = JobRunning
		rec.mu.Unlock()

		jerr := fn()

		rec.mu.Lock()
		if jerr != nil {
			rec.Status = JobFailed
			rec.Err = jerr.Error()
		} else {
			rec.Status = JobSucceeded
		}
		rec.mu.Unlock()
		close(h.ch)

		if s.bus != nil {
			kind := event.KindJobExecuted
			s.bus.Emit(kind, event.Detail{
				Scope: event.ScopeSystem, Tenant: tenant, JobID: id, Name: name,
			})
		}
	})
	return h, nil
}

// Job returns a copy of one job's state and whether it exists.
func (s *Scheduler) Job(id string) (Job, bool) {
	s.mu.Lock()
	rec, ok := s.jobs[id]
	s.mu.Unlock()
	if !ok {
		return Job{}, false
	}
	rec.mu.Lock()
	defer rec.mu.Unlock()
	return rec.Job, true
}

// Jobs returns snapshots of all jobs.
func (s *Scheduler) Jobs() []Job {
	s.mu.Lock()
	recs := make([]*jobRec, 0, len(s.jobs))
	for _, r := range s.jobs {
		recs = append(recs, r)
	}
	s.mu.Unlock()
	out := make([]Job, 0, len(recs))
	for _, r := range recs {
		r.mu.Lock()
		out = append(out, r.Job)
		r.mu.Unlock()
	}
	return out
}

// CountJobs reports how many jobs have been recorded (including rejected).
func (s *Scheduler) CountJobs() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.jobs)
}
