package engine

import (
	"context"
	"encoding/hex"
	"errors"
	"fmt"
)

// cancelCheckInterval is how often burnCPU polls for cancellation.
const cancelCheckInterval = 4096

// burnCPU performs deterministic CPU-bound work: an FNV-style mixing loop.
// The result depends only on work, which makes cross-tenant cache isolation
// assertions exact. Cancellation is polled every cancelCheckInterval steps.
func burnCPU(ctx context.Context, work int) (uint64, error) {
	var h uint64 = 1469598103934665603
	for i := 0; i < work; i++ {
		if i%cancelCheckInterval == 0 {
			select {
			case <-ctx.Done():
				return 0, ctx.Err()
			default:
			}
		}
		h ^= uint64(i) * 1099511628211
		h *= 1099511628211
	}
	return h, nil
}

// run executes one job: reserves memory, burns CPU, writes the result to the
// tenant-scoped cache key, and finalizes state. It never mutates another
// tenant's data — the cache key was bound to the job's tenant at submit time.
func (s *Scheduler) run(j *Job) {
	defer s.finish(j)

	if j.cancelled() {
		j.finalize(StatusCancelled, "", "cancelled before start", s.clk.Now())
		s.mu.Lock()
		s.tenantStatsLocked(j.TenantID).Cancelled++
		s.mu.Unlock()
		return
	}
	j.setRunning(s.clk.Now())

	// The memory budget is real: the job's declared bytes are allocated and
	// held for the duration of execution, and the scheduler accounted them
	// against the tenant's in-flight budget before dispatch.
	var mem []byte
	if j.MemBytes > 0 {
		mem = make([]byte, j.MemBytes)
		mem[0] = 1
		mem[len(mem)-1] = 1
	}

	h, err := burnCPU(j.ctx, j.Work)
	if err != nil {
		if errors.Is(err, context.Canceled) {
			j.finalize(StatusCancelled, "", "cancelled while running", s.clk.Now())
			s.mu.Lock()
			s.tenantStatsLocked(j.TenantID).Cancelled++
			s.mu.Unlock()
			return
		}
		j.finalize(StatusFailed, "", err.Error(), s.clk.Now())
		s.mu.Lock()
		s.tenantStatsLocked(j.TenantID).Failed++
		s.mu.Unlock()
		return
	}

	result := hex.EncodeToString([]byte(fmt.Sprintf("%016x", h)))
	// Persist under the tenant-scoped key. Use a fresh context: a client
	// disconnect after the compute finished must not lose the result.
	if err := s.cache.Put(context.Background(), j.TenantID, j.Key, []byte(result)); err != nil {
		j.finalize(StatusFailed, "", fmt.Sprintf("cache write: %v", err), s.clk.Now())
		s.mu.Lock()
		s.tenantStatsLocked(j.TenantID).Failed++
		s.mu.Unlock()
		return
	}
	_ = mem // held until here so the allocation spans the compute
	j.finalize(StatusCompleted, result, "", s.clk.Now())
	s.mu.Lock()
	s.tenantStatsLocked(j.TenantID).Completed++
	s.mu.Unlock()
}
