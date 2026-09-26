// Package jobs implements per-tenant execution isolation: each tenant
// gets its own FIFO queue and memory budget, a shared worker pool is
// scheduled round-robin across tenants, and jobs can be canceled by
// their owning tenant.
package jobs

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"example.com/tenantiso/internal/auth"
	"example.com/tenantiso/internal/backend"
	"example.com/tenantiso/internal/cache"
	"example.com/tenantiso/internal/clock"
)

// Admission rejections.
var (
	ErrQueueFull  = errors.New("tenant queue full")
	ErrMemBudget  = errors.New("tenant memory budget exceeded")
	ErrNotFound   = errors.New("job not found")
	ErrNotOwned   = errors.New("job belongs to another tenant")
	ErrTerminated = errors.New("job already in terminal state")
)

// Status is the lifecycle state of a job.
type Status string

// Job lifecycle states.
const (
	StatusQueued    Status = "queued"
	StatusRunning   Status = "running"
	StatusSucceeded Status = "succeeded"
	StatusFailed    Status = "failed"
	StatusCanceled  Status = "canceled"
)

// Config tunes the isolation limits.
type Config struct {
	Workers            int   // shared worker-pool size
	MaxQueuePerTenant  int   // max in-flight (queued+running) jobs per tenant
	MemBudgetPerTenant int64 // max in-flight memBytes per tenant
}

// Job is one CPU compute request.
type Job struct {
	ID         string    `json:"id"`
	TenantID   string    `json:"tenant_id"`
	Key        string    `json:"key"`
	Payload    string    `json:"-"`
	Iterations int       `json:"iterations"`
	MemBytes   int64     `json:"mem_bytes"`
	Status     Status    `json:"status"`
	Err        string    `json:"error,omitempty"`
	CreatedAt  time.Time `json:"created_at"`
	StartedAt  time.Time `json:"started_at,omitempty"`
	FinishedAt time.Time `json:"finished_at,omitempty"`

	cancel context.CancelFunc
}

// Manager owns all tenant queues, budgets, and the worker pool.
type Manager struct {
	cfg     Config
	clk     clock.Clock
	backend *backend.Fake
	cache   *cache.Cache

	mu        sync.Mutex
	jobsByID  map[string]*Job
	queues    map[string][]*Job // tenant -> FIFO of queued jobs
	memInUse  map[string]int64  // tenant -> reserved bytes (queued+running)
	inFlight  map[string]int    // tenant -> queued+running count
	nextID    int
	notify    chan struct{}
	workerSem chan struct{}
	closed    chan struct{}
	closeOnce sync.Once
	schedDone chan struct{}
}

// NewManager creates a Manager and starts its scheduler.
func NewManager(cfg Config, clk clock.Clock, be *backend.Fake, c *cache.Cache) *Manager {
	m := &Manager{
		cfg:       cfg,
		clk:       clk,
		backend:   be,
		cache:     c,
		jobsByID:  make(map[string]*Job),
		queues:    make(map[string][]*Job),
		memInUse:  make(map[string]int64),
		inFlight:  make(map[string]int),
		notify:    make(chan struct{}, 1),
		workerSem: make(chan struct{}, cfg.Workers),
		closed:    make(chan struct{}),
		schedDone: make(chan struct{}),
	}
	go m.scheduleLoop()
	return m
}

// Submit validates admission limits for the tenant and enqueues a job.
// The tenant ID comes only from the authenticated context.
func (m *Manager) Submit(ctx context.Context, key, payload string, iterations int, memBytes int64) (*Job, error) {
	tenantID, ok := auth.TenantFrom(ctx)
	if !ok {
		return nil, errors.New("no tenant in context")
	}
	m.mu.Lock()
	if m.inFlight[tenantID] >= m.cfg.MaxQueuePerTenant {
		m.mu.Unlock()
		return nil, ErrQueueFull
	}
	if m.memInUse[tenantID]+memBytes > m.cfg.MemBudgetPerTenant {
		m.mu.Unlock()
		return nil, ErrMemBudget
	}
	m.nextID++
	job := &Job{
		ID:         fmt.Sprintf("job-%d", m.nextID),
		TenantID:   tenantID,
		Key:        key,
		Payload:    payload,
		Iterations: iterations,
		MemBytes:   memBytes,
		Status:     StatusQueued,
		CreatedAt:  m.clk.Now(),
	}
	m.jobsByID[job.ID] = job
	m.queues[tenantID] = append(m.queues[tenantID], job)
	m.memInUse[tenantID] += memBytes
	m.inFlight[tenantID]++
	out := *job // snapshot: the worker mutates the live job concurrently
	m.mu.Unlock()

	select {
	case m.notify <- struct{}{}:
	default:
	}
	return &out, nil
}

// Get returns the job if it belongs to the tenant in ctx.
func (m *Manager) Get(ctx context.Context, id string) (*Job, error) {
	tenantID, _ := auth.TenantFrom(ctx)
	m.mu.Lock()
	defer m.mu.Unlock()
	job, ok := m.jobsByID[id]
	if !ok {
		return nil, ErrNotFound
	}
	if job.TenantID != tenantID {
		return nil, ErrNotOwned
	}
	out := *job // snapshot under lock
	return &out, nil
}

// Cancel cancels a queued or running job owned by the tenant in ctx.
func (m *Manager) Cancel(ctx context.Context, id string) (*Job, error) {
	tenantID, _ := auth.TenantFrom(ctx)
	m.mu.Lock()
	job, ok := m.jobsByID[id]
	if !ok {
		m.mu.Unlock()
		return nil, ErrNotFound
	}
	if job.TenantID != tenantID {
		m.mu.Unlock()
		return nil, ErrNotOwned
	}
	switch job.Status {
	case StatusQueued:
		m.removeFromQueueLocked(job)
		m.finishLocked(job, StatusCanceled, "")
	case StatusRunning:
		job.cancel() // worker observes ctx.Done and finishes the job
	default:
		m.mu.Unlock()
		return nil, ErrTerminated
	}
	out := *job
	m.mu.Unlock()
	return &out, nil
}

// removeFromQueueLocked drops a queued job from its tenant deque.
func (m *Manager) removeFromQueueLocked(job *Job) {
	q := m.queues[job.TenantID]
	for i, j := range q {
		if j == job {
			m.queues[job.TenantID] = append(q[:i], q[i+1:]...)
			return
		}
	}
}

// finishLocked transitions a job to a terminal state and releases its
// tenant's memory and in-flight reservations.
func (m *Manager) finishLocked(job *Job, st Status, errMsg string) {
	job.Status = st
	job.Err = errMsg
	job.FinishedAt = m.clk.Now()
	m.memInUse[job.TenantID] -= job.MemBytes
	m.inFlight[job.TenantID]--
}

// scheduleLoop dispatches queued jobs to the shared worker pool,
// round-robin across tenants so one tenant cannot starve the others.
func (m *Manager) scheduleLoop() {
	defer close(m.schedDone)
	rr := []string{} // round-robin tenant order
	for {
		select {
		case <-m.closed:
			return
		case <-m.notify:
		}
		for {
			m.mu.Lock()
			// Rebuild the round-robin tenant list from non-empty queues.
			rr = rr[:0]
			for t, q := range m.queues {
				if len(q) > 0 {
					rr = append(rr, t)
				}
			}
			var next *Job
			for _, t := range rr {
				if q := m.queues[t]; len(q) > 0 {
					next = q[0]
					m.queues[t] = q[1:]
					break
				}
			}
			if next == nil {
				m.mu.Unlock()
				break
			}
			m.mu.Unlock()

			select {
			case m.workerSem <- struct{}{}:
			case <-m.closed:
				return
			}
			go m.run(next)
		}
	}
}

// run executes one job against the fake backend and caches the result
// under the tenant-scoped composite key.
func (m *Manager) run(job *Job) {
	defer func() { <-m.workerSem }()
	ctx, cancel := context.WithCancel(context.Background())
	m.mu.Lock()
	if job.Status != StatusQueued { // canceled between dequeue and run
		m.mu.Unlock()
		cancel()
		return
	}
	job.Status = StatusRunning
	job.StartedAt = m.clk.Now()
	job.cancel = cancel
	m.mu.Unlock()

	result, err := m.backend.Compute(ctx, job.Payload, job.Iterations)

	m.mu.Lock()
	defer m.mu.Unlock()
	if job.Status == StatusCanceled {
		return
	}
	switch {
	case errors.Is(err, context.Canceled) || ctx.Err() != nil:
		m.finishLocked(job, StatusCanceled, "")
	case err != nil:
		m.finishLocked(job, StatusFailed, err.Error())
	default:
		m.cache.Set(job.TenantID, job.Key, result)
		m.finishLocked(job, StatusSucceeded, "")
	}
}

// Close stops the scheduler.
func (m *Manager) Close() {
	m.closeOnce.Do(func() {
		close(m.closed)
		<-m.schedDone
	})
}
