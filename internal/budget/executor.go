package budget

import (
	"context"
	"time"
)

// Executor runs an admitted job. Implementations are replaceable: tests use
// an executor that records invocations deterministically; production can run
// the job in a goroutine, inline, or on a worker pool.
type Executor interface {
	Execute(ctx context.Context, job Job) error
}

// Job is a unit of work admitted by the scheduler.
type Job func(ctx context.Context) error

// JobFunc adapts a plain executor function.
type JobFunc func(ctx context.Context, job Job) error

// Execute implements Executor.
func (f JobFunc) Execute(ctx context.Context, job Job) error { return f(ctx, job) }

// InlineExecutor runs the job synchronously in the calling goroutine. It is
// deterministic and needs no extra concurrency, so it is the default and is
// what the HTTP handler uses (the request goroutine is already per-request).
type InlineExecutor struct{}

// Execute implements Executor.
func (InlineExecutor) Execute(ctx context.Context, job Job) error { return job(ctx) }

// Receipt is the result of Scheduler.Submit.
type Receipt struct {
	Decision Decision  `json:"decision"`
	Start    time.Time `json:"start"`
	End      time.Time `json:"end,omitempty"`
	Err      string    `json:"error,omitempty"`
}

// Scheduler ties admission (the two-layer token decision) to execution.
type Scheduler struct {
	limiter  *Limiter
	executor Executor
	sink     Sink
}

// Limiter exposes the underlying limiter.
func (s *Scheduler) Limiter() *Limiter { return s.limiter }

// NewScheduler builds a scheduler with a replaceable executor. A nil
// executor defaults to InlineExecutor.
func NewScheduler(l *Limiter, ex Executor, sink Sink) *Scheduler {
	if ex == nil {
		ex = InlineExecutor{}
	}
	if sink == nil {
		sink = DiscardSink{}
	}
	return &Scheduler{limiter: l, executor: ex, sink: sink}
}

// Submit acquires cost tokens from both layers (blocking according to ctx),
// and on success runs the job via the configured executor. A denied or
// timed-out admission never executes the job and never partially consumes
// tokens.
func (s *Scheduler) Submit(ctx context.Context, tenant string, costMicro int64, job Job) Receipt {
	d, err := s.limiter.Acquire(ctx, tenant, costMicro)
	start := time.Time{}
	if d.At != (time.Time{}) {
		start = d.At
	}
	if err != nil || !d.Allowed {
		r := Receipt{Decision: d, Start: start}
		if err != nil {
			r.Err = err.Error()
		}
		return r
	}
	execErr := s.executor.Execute(ctx, job)
	end := s.limiter.clk.Now()
	r := Receipt{Decision: d, Start: start, End: end}
	if execErr != nil {
		r.Err = execErr.Error()
	}
	s.sink.Record(Event{
		Type:      EventExecute,
		At:        start,
		RequestID: d.RequestID,
		Tenant:    tenant,
		Result:    Allowed,
		Cost:      d.Cost,
		Detail:    r.Err,
	})
	return r
}
