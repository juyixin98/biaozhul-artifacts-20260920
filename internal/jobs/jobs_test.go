package jobs_test

import (
	"context"
	"testing"
	"time"

	"example.com/tenantiso/internal/auth"
	"example.com/tenantiso/internal/backend"
	"example.com/tenantiso/internal/cache"
	"example.com/tenantiso/internal/clock"
	"example.com/tenantiso/internal/jobs"
)

// newBlockedMgr builds a manager whose fake backend is stalled by
// injected latency, so submitted jobs stay queued/running and admission
// limits can be asserted deterministically.
func newBlockedMgr(t *testing.T, cfg jobs.Config) (*jobs.Manager, *backend.Fake, *clock.Fake) {
	t.Helper()
	clk := clock.NewFake(time.Date(2026, 9, 25, 0, 0, 0, 0, time.UTC))
	be := backend.New()
	be.SetFaults(backend.Faults{Latency: 500 * time.Millisecond})
	m := jobs.NewManager(cfg, clk, be, cache.New())
	t.Cleanup(m.Close)
	return m, be, clk
}

func TestQueueFullPerTenant(t *testing.T) {
	m, _, _ := newBlockedMgr(t, jobs.Config{Workers: 1, MaxQueuePerTenant: 1, MemBudgetPerTenant: 1 << 20})
	a := auth.WithTenant(context.Background(), "tenant-a")
	b := auth.WithTenant(context.Background(), "tenant-b")

	if _, err := m.Submit(a, "k1", "p", 1, 1); err != nil {
		t.Fatalf("first submit: %v", err)
	}
	if _, err := m.Submit(a, "k2", "p", 1, 1); err != jobs.ErrQueueFull {
		t.Fatalf("second submit: %v", err)
	}
	if _, err := m.Submit(b, "k3", "p", 1, 1); err != nil {
		t.Fatalf("other tenant must be admitted: %v", err)
	}
}

func TestMemBudgetPerTenant(t *testing.T) {
	m, _, _ := newBlockedMgr(t, jobs.Config{Workers: 1, MaxQueuePerTenant: 10, MemBudgetPerTenant: 100})
	a := auth.WithTenant(context.Background(), "tenant-a")
	if _, err := m.Submit(a, "k", "p", 1, 101); err != jobs.ErrMemBudget {
		t.Fatalf("over budget: %v", err)
	}
	if _, err := m.Submit(a, "k", "p", 1, 100); err != nil {
		t.Fatalf("at budget: %v", err)
	}
	if _, err := m.Submit(a, "k2", "p", 1, 1); err != jobs.ErrMemBudget {
		t.Fatalf("in-flight over budget: %v", err)
	}
}

func TestJobTimestampsUseClock(t *testing.T) {
	m, _, clk := newBlockedMgr(t, jobs.Config{Workers: 1, MaxQueuePerTenant: 4, MemBudgetPerTenant: 1 << 20})
	a := auth.WithTenant(context.Background(), "tenant-a")
	job, err := m.Submit(a, "k", "p", 1, 1)
	if err != nil {
		t.Fatalf("submit: %v", err)
	}
	if !job.CreatedAt.Equal(clk.Now()) {
		t.Fatalf("CreatedAt %v != clock %v", job.CreatedAt, clk.Now())
	}
}

func TestCancelQueuedJob(t *testing.T) {
	m, _, _ := newBlockedMgr(t, jobs.Config{Workers: 1, MaxQueuePerTenant: 4, MemBudgetPerTenant: 10})
	a := auth.WithTenant(context.Background(), "tenant-a")
	b := auth.WithTenant(context.Background(), "tenant-b")

	// Occupy the single worker so the next job stays queued.
	if _, err := m.Submit(a, "blocker", "p", 1, 5); err != nil {
		t.Fatalf("blocker: %v", err)
	}
	job, err := m.Submit(a, "k", "p", 1, 5)
	if err != nil {
		t.Fatalf("submit: %v", err)
	}
	if _, err := m.Cancel(b, job.ID); err != jobs.ErrNotOwned {
		t.Fatalf("cross-tenant cancel: %v", err)
	}
	canceled, err := m.Cancel(a, job.ID)
	if err != nil {
		t.Fatalf("cancel: %v", err)
	}
	if canceled.Status != jobs.StatusCanceled {
		t.Fatalf("status = %s", canceled.Status)
	}
	// Reservation released: the freed 5 bytes fit a new job.
	if _, err := m.Submit(a, "k2", "p", 1, 5); err != nil {
		t.Fatalf("resubmit after cancel: %v", err)
	}
	if _, err := m.Cancel(a, job.ID); err != jobs.ErrTerminated {
		t.Fatalf("double cancel: %v", err)
	}
}
