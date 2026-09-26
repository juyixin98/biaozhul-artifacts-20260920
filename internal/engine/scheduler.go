// Package engine implements per-tenant job queues with per-tenant memory
// budgets, round-robin scheduling across tenants, and cancellable CPU-bound
// execution. One tenant's overload (full queue or exhausted budget) never
// blocks other tenants: the scheduler simply skips a saturated tenant.
package engine

import (
	"context"
	"errors"
	"fmt"
	"sync"

	"tenantiso/internal/clock"
)

// Limits bound what a single tenant may consume.
type Limits struct {
	MaxQueueDepth       int   // queued jobs per tenant
	MaxInFlightJobs     int   // running jobs per tenant
	MaxInFlightMemBytes int64 // sum of MemBytes of running jobs per tenant
	MaxJobMemBytes      int64 // per-job memory ceiling
	MaxWork             int   // per-job work ceiling
}

// DefaultLimits are conservative values suitable for a local test service.
func DefaultLimits() Limits {
	return Limits{
		MaxQueueDepth:       4,
		MaxInFlightJobs:     2,
		MaxInFlightMemBytes: 16 << 20,
		MaxJobMemBytes:      8 << 20,
		MaxWork:             1_000_000_000,
	}
}

var (
	// ErrQueueFull means the tenant's queue is at capacity (HTTP 429).
	ErrQueueFull = errors.New("engine: tenant queue full")
	// ErrJobTooLarge means the job exceeds per-job ceilings (HTTP 422).
	ErrJobTooLarge = errors.New("engine: job exceeds per-job limits")
	// ErrStopped means the scheduler is shut down.
	ErrStopped = errors.New("engine: scheduler stopped")
)

// Cache is the write-through result store used on job completion.
type Cache interface {
	ScopedKey(tenant, key string) string
	Put(ctx context.Context, tenant, key string, val []byte) error
}

// TenantStats is a point-in-time view of one tenant's usage.
type TenantStats struct {
	Queued            int   `json:"queued"`
	InFlightJobs      int   `json:"in_flight_jobs"`
	InFlightMemBytes  int64 `json:"in_flight_mem_bytes"`
	Submitted         int64 `json:"submitted"`
	Completed         int64 `json:"completed"`
	Failed            int64 `json:"failed"`
	Cancelled         int64 `json:"cancelled"`
	RejectedQueueFull int64 `json:"rejected_queue_full"`
}

// Scheduler owns all tenant queues and the worker pool.
type Scheduler struct {
	clk     clock.Clock
	cache   Cache
	limits  Limits
	workers int

	mu           sync.Mutex
	cond         *sync.Cond
	queues       map[string][]*Job
	order        []string // tenant insertion order, scanned round-robin
	rr           int
	inFlightJobs map[string]int
	inFlightMem  map[string]int64
	jobs         map[string]*Job
	counter      int64
	stats        map[string]*TenantStats
	stopped      bool
	wg           sync.WaitGroup
}

// NewScheduler creates a scheduler; Start must be called to run workers.
func NewScheduler(clk clock.Clock, c Cache, limits Limits, workers int) *Scheduler {
	if workers < 1 {
		workers = 1
	}
	s := &Scheduler{
		clk:          clk,
		cache:        c,
		limits:       limits,
		workers:      workers,
		queues:       make(map[string][]*Job),
		inFlightJobs: make(map[string]int),
		inFlightMem:  make(map[string]int64),
		jobs:         make(map[string]*Job),
		stats:        make(map[string]*TenantStats),
	}
	s.cond = sync.NewCond(&s.mu)
	return s
}

// Start launches the worker pool.
func (s *Scheduler) Start() {
	for i := 0; i < s.workers; i++ {
		s.wg.Add(1)
		go s.worker()
	}
}

// Stop cancels queued jobs and waits for workers to exit. Running jobs are
// cancelled via their contexts.
func (s *Scheduler) Stop() {
	s.mu.Lock()
	s.stopped = true
	for _, j := range s.jobs {
		j.Cancel()
	}
	s.cond.Broadcast()
	s.mu.Unlock()
	s.wg.Wait()
}

// Submit enqueues a job for tenant. The tenant string must come from the
// authenticated context; the scheduler trusts it as the isolation boundary.
func (s *Scheduler) Submit(tenant, key string, work int, memBytes int64) (*Job, error) {
	if work < 1 || work > s.limits.MaxWork || memBytes < 0 || memBytes > s.limits.MaxJobMemBytes {
		return nil, ErrJobTooLarge
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.stopped {
		return nil, ErrStopped
	}
	st := s.tenantStatsLocked(tenant)
	if len(s.queues[tenant]) >= s.limits.MaxQueueDepth {
		st.RejectedQueueFull++
		return nil, ErrQueueFull
	}
	s.counter++
	ctx, cancel := context.WithCancel(context.Background())
	j := &Job{
		ID:        fmt.Sprintf("job-%06d", s.counter),
		TenantID:  tenant,
		Key:       key,
		CacheKey:  s.cache.ScopedKey(tenant, key),
		Work:      work,
		MemBytes:  memBytes,
		status:    StatusQueued,
		createdAt: s.clk.Now(),
		ctx:       ctx,
		cancel:    cancel,
		done:      make(chan struct{}),
	}
	if _, seen := s.queues[tenant]; !seen {
		s.order = append(s.order, tenant)
	}
	s.queues[tenant] = append(s.queues[tenant], j)
	s.jobs[j.ID] = j
	st.Submitted++
	s.cond.Broadcast()
	return j, nil
}

// Get returns the job if it belongs to tenant (jobs are not visible across
// tenants, mirroring the cache isolation rule).
func (s *Scheduler) Get(id, tenant string) (*Job, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	j, ok := s.jobs[id]
	if !ok || j.TenantID != tenant {
		return nil, false
	}
	return j, true
}

// Cancel marks the job cancelled. Queued jobs are finalized immediately;
// running jobs observe ctx cancellation inside the executor.
func (s *Scheduler) Cancel(id, tenant string) (*Job, bool) {
	s.mu.Lock()
	j, ok := s.jobs[id]
	if !ok || j.TenantID != tenant {
		s.mu.Unlock()
		return nil, false
	}
	j.Cancel()
	if snap := j.Snapshot(); snap.Status == StatusQueued {
		j.finalize(StatusCancelled, "", "cancelled while queued", s.clk.Now())
		s.tenantStatsLocked(tenant).Cancelled++
		s.removeFromQueueLocked(j)
	}
	s.cond.Broadcast()
	s.mu.Unlock()
	return j, true
}

func (s *Scheduler) removeFromQueueLocked(j *Job) {
	q := s.queues[j.TenantID]
	for i, candidate := range q {
		if candidate == j {
			s.queues[j.TenantID] = append(q[:i], q[i+1:]...)
			return
		}
	}
}

// Stats returns a copy of per-tenant statistics.
func (s *Scheduler) Stats() map[string]TenantStats {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make(map[string]TenantStats, len(s.stats))
	for t, st := range s.stats {
		cp := *st
		cp.Queued = len(s.queues[t])
		cp.InFlightJobs = s.inFlightJobs[t]
		cp.InFlightMemBytes = s.inFlightMem[t]
		out[t] = cp
	}
	return out
}

func (s *Scheduler) tenantStatsLocked(tenant string) *TenantStats {
	st, ok := s.stats[tenant]
	if !ok {
		st = &TenantStats{}
		s.stats[tenant] = st
	}
	return st
}

// tryTakeLocked picks the next runnable job, scanning tenants round-robin.
// A tenant at its in-flight job or memory budget is skipped, not blocking
// others. Cancelled queue heads are reaped as they are found.
func (s *Scheduler) tryTakeLocked() *Job {
	n := len(s.order)
	for i := 0; i < n; i++ {
		idx := (s.rr + i) % n
		tenant := s.order[idx]
		q := s.queues[tenant]
		for len(q) > 0 && q[0].cancelled() {
			head := q[0]
			q = q[1:]
			s.queues[tenant] = q
			head.finalize(StatusCancelled, "", "cancelled while queued", s.clk.Now())
			s.tenantStatsLocked(tenant).Cancelled++
		}
		if len(q) == 0 {
			continue
		}
		j := q[0]
		if s.inFlightJobs[tenant] >= s.limits.MaxInFlightJobs {
			continue
		}
		if s.inFlightMem[tenant]+j.MemBytes > s.limits.MaxInFlightMemBytes {
			continue
		}
		s.queues[tenant] = q[1:]
		s.inFlightJobs[tenant]++
		s.inFlightMem[tenant] += j.MemBytes
		s.rr = (idx + 1) % n
		return j
	}
	return nil
}

func (s *Scheduler) worker() {
	defer s.wg.Done()
	for {
		s.mu.Lock()
		for {
			if s.stopped {
				s.mu.Unlock()
				return
			}
			if j := s.tryTakeLocked(); j != nil {
				s.mu.Unlock()
				s.run(j)
				break
			}
			s.cond.Wait()
		}
	}
}

// finishLocked releases the tenant's in-flight accounting for j.
func (s *Scheduler) finish(j *Job) {
	s.mu.Lock()
	s.inFlightJobs[j.TenantID]--
	s.inFlightMem[j.TenantID] -= j.MemBytes
	s.cond.Broadcast()
	s.mu.Unlock()
}
