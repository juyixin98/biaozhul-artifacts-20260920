package engine

import (
	"context"
	"sync"
	"time"
)

// Status is the lifecycle state of a Job.
type Status string

const (
	StatusQueued    Status = "queued"
	StatusRunning   Status = "running"
	StatusCompleted Status = "completed"
	StatusFailed    Status = "failed"
	StatusCancelled Status = "cancelled"
)

// terminal reports whether s is a final state.
func (s Status) terminal() bool {
	return s == StatusCompleted || s == StatusFailed || s == StatusCancelled
}

// Job is one CPU-compute request. Identity fields (TenantID) come from the
// authenticated context at submit time and are immutable afterwards.
type Job struct {
	ID       string
	TenantID string
	Key      string
	CacheKey string
	Work     int
	MemBytes int64

	mu         sync.Mutex
	status     Status
	result     string
	err        string
	createdAt  time.Time
	startedAt  time.Time
	finishedAt time.Time

	ctx    context.Context
	cancel context.CancelFunc
	done   chan struct{} // closed exactly once when the job reaches a terminal state
}

// Snapshot is an immutable view of a Job for API responses.
type Snapshot struct {
	ID         string    `json:"job_id"`
	TenantID   string    `json:"tenant_id"`
	Key        string    `json:"key"`
	CacheKey   string    `json:"cache_key"`
	Work       int       `json:"work"`
	MemBytes   int64     `json:"mem_bytes"`
	Status     Status    `json:"status"`
	Result     string    `json:"result,omitempty"`
	Error      string    `json:"error,omitempty"`
	CreatedAt  time.Time `json:"created_at"`
	StartedAt  time.Time `json:"started_at,omitempty"`
	FinishedAt time.Time `json:"finished_at,omitempty"`
}

// Snapshot returns a consistent copy of the job's observable state.
func (j *Job) Snapshot() Snapshot {
	j.mu.Lock()
	defer j.mu.Unlock()
	return Snapshot{
		ID:         j.ID,
		TenantID:   j.TenantID,
		Key:        j.Key,
		CacheKey:   j.CacheKey,
		Work:       j.Work,
		MemBytes:   j.MemBytes,
		Status:     j.status,
		Result:     j.result,
		Error:      j.err,
		CreatedAt:  j.createdAt,
		StartedAt:  j.startedAt,
		FinishedAt: j.finishedAt,
	}
}

// Done closes when the job reaches a terminal state.
func (j *Job) Done() <-chan struct{} { return j.done }

// Cancel requests cancellation; safe to call multiple times.
func (j *Job) Cancel() { j.cancel() }

func (j *Job) setRunning(at time.Time) {
	j.mu.Lock()
	defer j.mu.Unlock()
	j.status = StatusRunning
	j.startedAt = at
}

// finalize sets a terminal state and closes done. The first call wins.
func (j *Job) finalize(status Status, result, errMsg string, at time.Time) {
	j.mu.Lock()
	defer j.mu.Unlock()
	if j.status.terminal() {
		return
	}
	j.status = status
	j.result = result
	j.err = errMsg
	j.finishedAt = at
	close(j.done)
}

func (j *Job) cancelled() bool {
	select {
	case <-j.ctx.Done():
		return true
	default:
		return false
	}
}
