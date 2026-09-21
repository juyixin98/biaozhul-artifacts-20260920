package worker

import (
	"context"
	"sync"
	"testing"
	"time"

	"sitevitals/internal/collector"
	"sitevitals/internal/config"
	"sitevitals/internal/models"
	"sitevitals/internal/store"
)

type configType = config.Config

// fakeExec returns scripted results/failures per target URL.
type fakeExec struct {
	mu      sync.Mutex
	results map[string]scripted
	calls   map[string]int
	// hangs blocks until the channel for that URL is closed.
	hangs map[string]chan struct{}
}

type scripted struct {
	err *collector.CollectError
	res *collector.Result
}

func (f *fakeExec) Collect(ctx context.Context, targetURL string, opt collector.Options) (*collector.Result, error) {
	f.mu.Lock()
	f.calls[targetURL]++
	hang := f.hangs[targetURL]
	s := f.results[targetURL]
	f.mu.Unlock()
	if hang != nil {
		select {
		case <-hang:
		case <-ctx.Done():
			return nil, &collector.CollectError{Class: models.FailBrowserExited, Message: "fake worker cancelled: " + ctx.Err().Error()}
		}
	}
	if s.err != nil {
		return nil, s.err
	}
	if s.res != nil {
		return s.res, nil
	}
	return &collector.Result{FinalURL: targetURL, Metrics: goodMetrics()}, nil
}

func goodMetrics() []*models.Metric {
	v := 10.0
	return []*models.Metric{
		{Name: collector.MetricFCP, Status: models.MetricCollected, ValueMS: &v},
		{Name: collector.MetricCLS, Status: models.MetricCollected, ValueCLS: &v},
	}
}

func newFakeExec() *fakeExec {
	return &fakeExec{
		results: map[string]scripted{},
		calls:   map[string]int{},
		hangs:   map[string]chan struct{}{},
	}
}

func seedChecker(t *testing.T, repo *store.Repo) {
	t.Helper()
	ctx := context.Background()
	site := &models.Site{Name: "x", SchemeHost: "http://x.test", Enabled: true}
	if err := repo.CreateSite(ctx, site); err != nil {
		t.Fatal(err)
	}
	if err := repo.CreateAllowedURL(ctx, &models.AllowedURL{
		SiteID: site.ID, URLPattern: "http://x.test/*", Enabled: true,
	}); err != nil {
		t.Fatal(err)
	}
}

func waitForStatus(t *testing.T, repo *store.Repo, jobID uint64, wantStatus string, timeout time.Duration) *models.Job {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		j, err := repo.GetJob(context.Background(), jobID)
		if err != nil {
			t.Fatal(err)
		}
		if j.Status == wantStatus {
			return j
		}
		time.Sleep(30 * time.Millisecond)
	}
	j, _ := repo.GetJob(context.Background(), jobID)
	t.Fatalf("job %d status=%s want %s (last_error=%s)", jobID, j.Status, wantStatus, j.LastError)
	return nil
}

func waitForAttempt(t *testing.T, repo *store.Repo, jobID uint64, timeout time.Duration) *models.Run {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		runs, err := repo.ListRunsForJob(context.Background(), jobID)
		if err != nil {
			t.Fatal(err)
		}
		if len(runs) > 0 {
			return &runs[0]
		}
		time.Sleep(15 * time.Millisecond)
	}
	t.Fatalf("no attempt for job %d within %v", jobID, timeout)
	return nil
}

func TestSuccessAndFailingJobDoesNotBlock(t *testing.T) {
	db := storeTestDB(t)
	exec := newFakeExec()
	cfg := configType{
		Workers: 2, MaxBrowsers: 2, LeaseTTL: 2 * time.Second,
		HeartbeatInterval: 500 * time.Millisecond, ReapInterval: 300 * time.Millisecond,
	}
	_, q, repo, stop := runningPool(t, cfg, db, exec)
	defer stop()
	seedChecker(t, repo)
	ctx := context.Background()

	// First job always fails navigation (all 3 attempts exhausted).
	exec.results["http://x.test/hang-page"] = scripted{
		err: &collector.CollectError{Class: models.FailNavigationTimeout, Message: "timeout"},
	}
	bad, err := q.Enqueue(ctx, "http://x.test/hang-page", models.ViewportDesktop, 3)
	if err != nil {
		t.Fatal(err)
	}
	// A good job enqueued right behind must still complete.
	good, err := q.Enqueue(ctx, "http://x.test/ok", models.ViewportDesktop, 1)
	if err != nil {
		t.Fatal(err)
	}

	waitForStatus(t, repo, good.ID, models.JobSucceeded, 10*time.Second)
	waitForStatus(t, repo, bad.ID, models.JobFailed, 20*time.Second)

	if got := exec.calls["http://x.test/hang-page"]; got != 3 {
		t.Errorf("bad job calls=%d want 3 retries", got)
	}
}

func TestCrashRecoveryAndFencing(t *testing.T) {
	db := storeTestDB(t)
	exec := newFakeExec()
	cfg := configType{
		Workers: 1, MaxBrowsers: 1, LeaseTTL: 400 * time.Millisecond,
		HeartbeatInterval: 30 * time.Second, // deliberately longer than TTL: no heartbeat = crash
		ReapInterval:      150 * time.Millisecond,
	}
	_, q, repo, stop := runningPool(t, cfg, db, exec)
	defer stop()
	seedChecker(t, repo)
	ctx := context.Background()

	// First attempt hangs forever (simulates a killed process mid-navigation);
	// later attempts succeed.
	release := make(chan struct{})
	exec.hangs["http://x.test/flaky"] = release
	exec.results["http://x.test/flaky"] = scripted{}

	job, err := q.Enqueue(ctx, "http://x.test/flaky", models.ViewportMobile, 3)
	if err != nil {
		t.Fatal(err)
	}

	attempt1 := waitForAttempt(t, repo, job.ID, 5*time.Second)
	// Give it time to start executing the hung collection.
	time.Sleep(200 * time.Millisecond)

	// Wait for the reaper to reclaim the expired lease back to the queue
	// (attempt 1 is marked failed) before letting the blocked call return.
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		j, err := repo.GetJob(ctx, job.ID)
		if err != nil {
			t.Fatal(err)
		}
		if j.Status == models.JobQueued && j.Attempts == 1 {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	a1, err := repo.GetRun(ctx, attempt1.ID)
	if err != nil {
		t.Fatal(err)
	}
	if a1.Status != models.RunFailed {
		// Reaper must have marked attempt 1 failed before we proceed.
		t.Fatalf("attempt1 status=%s want failed after reaping", a1.Status)
	}
	close(release)

	final := waitForStatus(t, repo, job.ID, models.JobSucceeded, 10*time.Second)
	if *final.SucceededRunID == attempt1.ID {
		t.Fatal("succeeded run must be a later attempt, not the crashed attempt 1")
	}
}

func TestPolicyViolationFailsFast(t *testing.T) {
	db := storeTestDB(t)
	exec := newFakeExec()
	cfg := configType{
		Workers: 1, MaxBrowsers: 1, LeaseTTL: 5 * time.Second,
		HeartbeatInterval: time.Second, ReapInterval: time.Second,
	}
	_, q, repo, stop := runningPool(t, cfg, db, exec)
	defer stop()
	// No sites/rules seeded: checker denies everything.
	ctx := context.Background()

	// Insert a job directly (API would reject it) to exercise execution-time policy.
	job, err := q.Enqueue(ctx, "http://forbidden.test/x", models.ViewportDesktop, 1)
	if err != nil {
		t.Fatal(err)
	}
	j := waitForStatus(t, repo, job.ID, models.JobFailed, 8*time.Second)
	if j.LastFailClass != models.FailPolicy {
		t.Fatalf("fail class=%s want %s", j.LastFailClass, models.FailPolicy)
	}
	if got := exec.calls["http://forbidden.test/x"]; got != 0 {
		t.Fatalf("collector launches=%d must be 0 for a policy-denied target", got)
	}
}
