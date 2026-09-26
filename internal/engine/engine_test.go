package engine

import (
	"context"
	"sync"
	"testing"
	"time"

	"tenantiso/internal/clock"
)

type memCache struct {
	mu   sync.Mutex
	puts map[string][]byte
}

func newMemCache() *memCache { return &memCache{puts: make(map[string][]byte)} }

func (m *memCache) ScopedKey(tenant, key string) string { return "compute|" + tenant + "|" + key }

func (m *memCache) Put(_ context.Context, tenant, key string, val []byte) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.puts[m.ScopedKey(tenant, key)] = val
	return nil
}

func testLimits() Limits {
	return Limits{
		MaxQueueDepth:       2,
		MaxInFlightJobs:     1,
		MaxInFlightMemBytes: 1024,
		MaxJobMemBytes:      4096,
		MaxWork:             100_000_000,
	}
}

func newTestScheduler(t *testing.T, limits Limits, workers int) *Scheduler {
	t.Helper()
	s := NewScheduler(clock.Real{}, newMemCache(), limits, workers)
	s.Start()
	t.Cleanup(s.Stop)
	return s
}

func awaitStatus(t *testing.T, j *Job, want Status, timeout time.Duration) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if got := j.Snapshot().Status; got == want {
			return
		}
		time.Sleep(2 * time.Millisecond)
	}
	t.Fatalf("job %s did not reach %s within %s (now %s)", j.ID, want, timeout, j.Snapshot().Status)
}

func TestSubmitCompletes(t *testing.T) {
	s := newTestScheduler(t, testLimits(), 1)
	j, err := s.Submit("t1", "k", 1000, 128)
	if err != nil {
		t.Fatalf("submit: %v", err)
	}
	awaitStatus(t, j, StatusCompleted, 5*time.Second)
	if j.Snapshot().Result == "" {
		t.Fatal("expected a result")
	}
}

func TestQueueFullRejectsOnlyThatTenant(t *testing.T) {
	s := newTestScheduler(t, testLimits(), 1)
	// Fill t1 with long jobs until the queue rejects; whether the worker has
	// already picked up the first job is a race, so accept either boundary.
	accepted := 0
	var fullErr error
	for i := 0; i < 10; i++ {
		_, err := s.Submit("t1", "k", 50_000_000, 128)
		if err == ErrQueueFull {
			fullErr = err
			break
		}
		if err != nil {
			t.Fatalf("submit %d: %v", i, err)
		}
		accepted++
	}
	if fullErr == nil {
		t.Fatal("expected ErrQueueFull after filling the tenant queue")
	}
	if accepted > testLimits().MaxQueueDepth+testLimits().MaxInFlightJobs {
		t.Fatalf("accepted %d jobs, exceeds queue+inflight capacity", accepted)
	}
	// t2 is unaffected by t1's full queue.
	j2, err := s.Submit("t2", "a", 1000, 128)
	if err != nil {
		t.Fatalf("t2 submit: %v", err)
	}
	awaitStatus(t, j2, StatusCompleted, 5*time.Second)
}

func TestMemoryBudgetSkipsSaturatedTenant(t *testing.T) {
	limits := testLimits()
	limits.MaxInFlightMemBytes = 512 // one 512-byte job at a time per tenant
	s := newTestScheduler(t, limits, 2)

	big, err := s.Submit("t1", "big", 50_000_000, 512)
	if err != nil {
		t.Fatalf("submit big: %v", err)
	}
	awaitStatus(t, big, StatusRunning, 5*time.Second)

	// t1's second job exceeds the remaining budget; it must stay queued
	// while t2's job still runs.
	blocked, err := s.Submit("t1", "blocked", 1000, 512)
	if err != nil {
		t.Fatalf("submit blocked: %v", err)
	}
	other, err := s.Submit("t2", "other", 1000, 512)
	if err != nil {
		t.Fatalf("submit other: %v", err)
	}
	awaitStatus(t, other, StatusCompleted, 5*time.Second)
	if got := blocked.Snapshot().Status; got != StatusQueued {
		t.Fatalf("expected t1 second job still queued (budget), got %s", got)
	}
	awaitStatus(t, big, StatusCompleted, 5*time.Second)
	awaitStatus(t, blocked, StatusCompleted, 5*time.Second)
}

func TestCancelQueuedJob(t *testing.T) {
	s := newTestScheduler(t, testLimits(), 1)
	if _, err := s.Submit("t1", "running", 50_000_000, 128); err != nil {
		t.Fatalf("submit running: %v", err)
	}
	queued, err := s.Submit("t1", "queued", 1000, 128)
	if err != nil {
		t.Fatalf("submit queued: %v", err)
	}
	if _, ok := s.Cancel(queued.ID, "t1"); !ok {
		t.Fatal("cancel failed")
	}
	awaitStatus(t, queued, StatusCancelled, 5*time.Second)
}

func TestCancelRunningJob(t *testing.T) {
	s := newTestScheduler(t, testLimits(), 1)
	j, err := s.Submit("t1", "long", 100_000_000, 128)
	if err != nil {
		t.Fatalf("submit: %v", err)
	}
	awaitStatus(t, j, StatusRunning, 5*time.Second)
	if _, ok := s.Cancel(j.ID, "t1"); !ok {
		t.Fatal("cancel failed")
	}
	awaitStatus(t, j, StatusCancelled, 5*time.Second)
}

func TestCancelRequiresOwningTenant(t *testing.T) {
	s := newTestScheduler(t, testLimits(), 1)
	j, err := s.Submit("t1", "k", 1000, 128)
	if err != nil {
		t.Fatalf("submit: %v", err)
	}
	if _, ok := s.Cancel(j.ID, "t2"); ok {
		t.Fatal("t2 must not cancel t1's job")
	}
	if _, ok := s.Get(j.ID, "t2"); ok {
		t.Fatal("t2 must not see t1's job")
	}
}

func TestJobTooLargeRejected(t *testing.T) {
	s := newTestScheduler(t, testLimits(), 1)
	if _, err := s.Submit("t1", "k", 1, 99999); err != ErrJobTooLarge {
		t.Fatalf("expected ErrJobTooLarge for mem, got %v", err)
	}
	if _, err := s.Submit("t1", "k", 0, 0); err != ErrJobTooLarge {
		t.Fatalf("expected ErrJobTooLarge for work=0, got %v", err)
	}
}

func TestFairnessAcrossTenants(t *testing.T) {
	limits := testLimits()
	limits.MaxQueueDepth = 8
	s := newTestScheduler(t, limits, 1)
	var jobs []*Job
	// Interleave submissions from two tenants.
	for i := 0; i < 4; i++ {
		j1, err := s.Submit("t1", "k", 2_000_000, 128)
		if err != nil {
			t.Fatalf("t1 submit: %v", err)
		}
		j2, err := s.Submit("t2", "k", 2_000_000, 128)
		if err != nil {
			t.Fatalf("t2 submit: %v", err)
		}
		jobs = append(jobs, j1, j2)
	}
	for _, j := range jobs {
		awaitStatus(t, j, StatusCompleted, 10*time.Second)
	}
	stats := s.Stats()
	if stats["t1"].Completed != 4 || stats["t2"].Completed != 4 {
		t.Fatalf("expected 4 completions each, got %+v", stats)
	}
}
