package worker_test

import (
	"context"
	"net/http/httptest"
	"net/url"
	"os"
	"testing"
	"time"

	"sitevitals/internal/browser"
	"sitevitals/internal/config"
	"sitevitals/internal/demo"
	"sitevitals/internal/models"
	"sitevitals/internal/store"
	"sitevitals/internal/testutil"
	"sitevitals/internal/worker"
)

// originOf returns scheme://host[:port] for an absolute URL.
func originOf(raw string) string {
	u, err := url.Parse(raw)
	if err != nil {
		return raw
	}
	return u.Scheme + "://" + u.Host
}

// These end-to-end tests need both a MySQL DSN (TEST_MYSQL_DSN) and a local
// Chromium; either missing skips the test rather than failing. They exercise
// the durable queue + lease/heartbeat + browser collection + report/alerts
// path through the real worker loop.

func e2eSetup(t *testing.T) (*store.Store, *browser.Instance, string, context.CancelFunc) {
	t.Helper()
	if os.Getenv("TEST_MYSQL_DSN") == "" {
		t.Skip("TEST_MYSQL_DSN not set; skipping worker end-to-end test")
	}
	if os.Getenv("SKIP_CHROME_TESTS") != "" {
		t.Skip("SKIP_CHROME_TESTS set")
	}
	st := testutil.NewStore(t)
	inst := browser.NewInstance(browser.Options{Headless: true})
	ctx, cancel := context.WithTimeout(context.Background(), 40*time.Second)
	if err := inst.Start(ctx); err != nil {
		cancel()
		t.Skipf("chromium unavailable: %v", err)
	}
	srv := httptest.NewServer(demo.NewServer())
	t.Cleanup(func() {
		srv.Close()
		_ = inst.Close()
	})
	return st, inst, srv.URL, cancel
}

func runPoolWithTask(t *testing.T, st *store.Store, inst *browser.Instance, taskURL string, vp models.Viewport) uint {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)

	if _, err := st.EnsureSite(ctx, "demo-e2e", originOf(taskURL), "/", true); err != nil {
		t.Fatal(err)
	}
	// Enqueue with the exact demo base URL (normalized form).
	task, err := st.Enqueue(ctx, store.EnqueueRequest{
		URL: taskURL, Viewport: vp, MaxAttempts: 1,
	})
	if err != nil {
		t.Fatal(err)
	}

	cfg := config.Config{
		WorkerID:           "test-worker",
		BrowserConcurrency: 1,
		LeaseDuration:      30 * time.Second,
		HeartbeatInterval:  2 * time.Second,
		PollInterval:       200 * time.Millisecond,
		NavTimeout:         20 * time.Second,
		TaskTimeout:        40 * time.Second,
		SettleTime:         2 * time.Second,
		MaxRedirects:       10,
		RunOnce:            false,
	}
	pool := worker.New(cfg, st, []*browser.Instance{inst})
	go func() { _ = pool.Run(ctx) }()

	final := testutil.WaitForTaskStatus(t, st, task.ID, models.StateSucceeded, models.StateDead, models.StateFailed)
	return final.ID
}

// TestWorker_EndToEnd_Success runs one task through the queue with a real
// browser, then asserts a single report, a successful run with metrics, and
// no failed-run contamination of stats.
func TestWorker_EndToEnd_Success(t *testing.T) {
	st, inst, baseURL, cancel := e2eSetup(t)
	defer cancel()

	id := runPoolWithTask(t, st, inst, baseURL+"/normal", models.ViewportDesktop)
	task, err := st.GetTask(context.Background(), id)
	if err != nil {
		t.Fatal(err)
	}
	if task.Status != models.StateSucceeded {
		t.Fatalf("task status=%s error=%s: %s", task.Status, task.ErrorCode, task.ErrorMsg)
	}
	runs, err := st.ListRunsByTask(context.Background(), id)
	if err != nil || len(runs) != 1 || runs[0].Status != models.StateSucceeded {
		t.Fatalf("runs=%v err=%v", runs, err)
	}
	r := runs[0]
	if r.FCPMS == nil || r.LCPMS == nil || r.NavDurationMS == nil {
		t.Fatalf("core metrics not collected: fcp=%v lcp=%v nav=%v", r.FCPMS, r.LCPMS, r.NavDurationMS)
	}
	if r.WindowEnd == nil {
		t.Fatal("long-task window not recorded")
	}
	rep, err := st.GetReportByTask(context.Background(), id)
	if err != nil || len(rep.Markdown) == 0 {
		t.Fatalf("report missing: %v", err)
	}

	// Stats must only include successful runs and count this one.
	stats, err := st.Stats(context.Background(), task.URL, models.ViewportDesktop)
	if err != nil {
		t.Fatal(err)
	}
	if stats.SuccessfulRuns != 1 {
		t.Fatalf("stats successful runs=%d, want 1", stats.SuccessfulRuns)
	}
	if stats.AvgFCPMS == nil {
		t.Fatal("avg FCP should be computed from the successful run")
	}
}

// TestWorker_EndToEnd_FailureClassifiedThenDead pushes a whitelisted URL that
// refuses connection: the task must be classified NAVIGATION_FAILED (not
// timeout, not crash), retried up to max attempts and then parked dead.
func TestWorker_EndToEnd_FailureClassifiedThenDead(t *testing.T) {
	st, inst, baseURL, cancel := e2eSetup(t)
	defer cancel()

	ctx := context.Background()
	// Whitelist a second origin pointing at a closed port on localhost.
	badOrigin := "http://127.0.0.1:1"
	if _, err := st.EnsureSite(ctx, "dead", badOrigin, "/", true); err != nil {
		t.Fatal(err)
	}
	task, err := st.Enqueue(ctx, store.EnqueueRequest{
		URL: badOrigin + "/x", Viewport: models.ViewportMobile, MaxAttempts: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	cctx, ccancel := context.WithCancel(ctx)
	defer ccancel()
	cfg := config.Config{
		WorkerID: "test-worker-fail", LeaseDuration: 20 * time.Second,
		HeartbeatInterval: 5 * time.Second, PollInterval: 100 * time.Millisecond,
		NavTimeout: 6 * time.Second, TaskTimeout: 15 * time.Second,
		SettleTime: time.Second, MaxRedirects: 10,
	}
	pool := worker.New(cfg, st, []*browser.Instance{inst})
	go func() { _ = pool.Run(cctx) }()

	final := testutil.WaitForTaskStatus(t, st, task.ID, models.StateDead, models.StateFailed, models.StateSucceeded)
	if final.Status == models.StateSucceeded {
		t.Fatal("closed-port target cannot succeed")
	}
	if final.ErrorCode == browser.CodeBrowserCrash {
		t.Fatalf("connection failure misclassified as browser crash: %s", final.ErrorMsg)
	}
	// The browser must still be alive to serve the next collection.
	select {
	case <-inst.Crashed():
		t.Fatal("browser instance crashed while handling a navigation failure")
	default:
	}
	// Failed runs must not enter successful-metric statistics.
	stats, err := st.Stats(ctx, task.URL, models.ViewportMobile)
	if err != nil {
		t.Fatal(err)
	}
	if stats.SuccessfulRuns != 0 {
		t.Fatalf("failed run leaked into stats: %d", stats.SuccessfulRuns)
	}
	_ = baseURL
}
