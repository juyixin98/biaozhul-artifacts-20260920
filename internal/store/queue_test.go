package store_test

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"sitevitals/internal/models"
	"sitevitals/internal/store"
	"sitevitals/internal/testutil"
)

func enqueueTask(t *testing.T, st *store.Store, urlSuffix string) *models.Task {
	t.Helper()
	task, err := st.Enqueue(context.Background(), store.EnqueueRequest{
		URL:         "http://demo.local/page/" + urlSuffix,
		Viewport:    models.ViewportDesktop,
		MaxAttempts: 3,
	})
	if err != nil {
		t.Fatalf("enqueue: %v", err)
	}
	return task
}

// TestClaim_Concurrent_ExactlyOneWinner verifies the SKIP LOCKED claim path:
// many workers racing for one task must produce exactly one owner.
func TestClaim_Concurrent_ExactlyOneWinner(t *testing.T) {
	st := testutil.NewStore(t)
	task := enqueueTask(t, st, "race")

	const n = 12
	var (
		wg      sync.WaitGroup
		mu      sync.Mutex
		winners []*store.ClaimResult
		barrier = make(chan struct{})
	)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			<-barrier
			c, err := st.Claim(context.Background(), owner(id), time.Minute)
			if err != nil {
				t.Errorf("worker %d claim: %v", id, err)
				return
			}
			if c != nil {
				mu.Lock()
				winners = append(winners, c)
				mu.Unlock()
			}
		}(i)
	}
	close(barrier)
	wg.Wait()

	if len(winners) != 1 {
		t.Fatalf("expected exactly 1 winner, got %d", len(winners))
	}
	got := testutil.WaitForTaskStatus(t, st, task.ID, models.StateRunning)
	if got.Attempts != 1 || got.Owner == nil {
		t.Fatalf("unclaimed state after race: %+v", got)
	}
	runs, err := st.ListRunsByTask(context.Background(), task.ID)
	if err != nil || len(runs) != 1 {
		t.Fatalf("expected exactly 1 run row, got %d (err=%v)", len(runs), err)
	}
}

// TestClaim_CrashRecovery_LeaseExpiry simulates a worker that died holding a
// lease: after expiry the task must be re-claimable, the stale run marked
// abandoned, and attempts incremented (no zombie owner).
func TestClaim_CrashRecovery_LeaseExpiry(t *testing.T) {
	st := testutil.NewStore(t)
	task := enqueueTask(t, st, "crash")

	first, err := st.Claim(context.Background(), "worker-A", 50*time.Millisecond)
	if err != nil || first == nil {
		t.Fatalf("first claim: %v", err)
	}
	// Worker A "dies": no heartbeat, no commit. Wait the lease out.
	time.Sleep(90 * time.Millisecond)

	second, err := st.Claim(context.Background(), "worker-B", time.Minute)
	if err != nil || second == nil {
		t.Fatalf("reclaim after expiry: %v", err)
	}
	if second.Run.ID == first.Run.ID || second.Run.AttemptNo != 2 {
		t.Fatalf("expected a fresh attempt #2, got run %d attempt %d", second.Run.ID, second.Run.AttemptNo)
	}
	if *second.Task.LeaseToken == *first.Task.LeaseToken {
		t.Fatal("lease token must rotate on reclaim")
	}

	runs, _ := st.ListRunsByTask(context.Background(), task.ID)
	var abandoned, running int
	for _, r := range runs {
		switch r.Status {
		case models.StateAbandoned:
			abandoned++
			if r.ErrorCode != "LEASE_EXPIRED" {
				t.Fatalf("abandoned run error code = %q", r.ErrorCode)
			}
		case models.StateRunning:
			running++
		}
	}
	if abandoned != 1 || running != 1 {
		t.Fatalf("expected 1 abandoned + 1 running run, got %d/%d", abandoned, running)
	}
}

// TestClaim_ReaperThenReclaim covers the path where the defensive reaper
// reset an expired running task back to queued before a worker claimed it:
// the stale running run must still be marked abandoned.
func TestClaim_ReaperThenReclaim(t *testing.T) {
	st := testutil.NewStore(t)
	task := enqueueTask(t, st, "reaper")

	first, err := st.Claim(context.Background(), "worker-A", 30*time.Millisecond)
	if err != nil || first == nil {
		t.Fatalf("claim: %v", err)
	}
	time.Sleep(60 * time.Millisecond)
	n, err := st.RequeueExpired(context.Background())
	if err != nil || n != 1 {
		t.Fatalf("reaper touched=%d err=%v", n, err)
	}
	second, err := st.Claim(context.Background(), "worker-B", time.Minute)
	if err != nil || second == nil {
		t.Fatalf("claim after reaper: %v", err)
	}
	if second.Run.AttemptNo != 2 {
		t.Fatalf("attempt = %d, want 2", second.Run.AttemptNo)
	}
	runs, _ := st.ListRunsByTask(context.Background(), task.ID)
	var abandoned int
	for _, r := range runs {
		if r.Status == models.StateAbandoned {
			abandoned++
		}
	}
	if abandoned != 1 {
		t.Fatalf("stale run not abandoned (count=%d)", abandoned)
	}
}

// TestLateCommit_RejectedByToken is the "old executor submits late" rule:
// after a reclaim the previous lease token must not be able to overwrite the
// new attempt's result in either direction.
func TestLateCommit_RejectedByToken(t *testing.T) {
	st := testutil.NewStore(t)
	task := enqueueTask(t, st, "late-commit")

	first, err := st.Claim(context.Background(), "worker-A", 30*time.Millisecond)
	if err != nil || first == nil {
		t.Fatalf("claim: %v", err)
	}
	oldToken := *first.Task.LeaseToken
	time.Sleep(60 * time.Millisecond)
	second, err := st.Claim(context.Background(), "worker-B", time.Minute)
	if err != nil || second == nil {
		t.Fatalf("reclaim: %v", err)
	}

	fcp := 123.4
	m := &store.RunMetrics{
		FCPMS: &fcp,
		MetricStatus: models.MetricSet{
			Navigation: models.MetricOK, FCP: models.MetricOK,
			LCP: models.MetricUnsupported, CLS: models.MetricUnsupported,
			LongTasks: models.MetricUnsupported, Resources: models.MetricOK,
		},
	}
	// Old executor tries to commit success with its dead token -> rejected.
	err = st.CommitSuccess(context.Background(), store.SuccessInput{
		TaskID: task.ID, Token: oldToken, RunID: first.Run.ID,
		FinalURL: task.URL, Collected: m, ReportMD: "stale",
	})
	if !errors.Is(err, store.ErrLeaseLost) {
		t.Fatalf("old success commit err = %v, want ErrLeaseLost", err)
	}
	// Old executor tries a failure too -> rejected, new run untouched.
	err = st.CommitFailure(context.Background(), store.FailureInput{
		TaskID: task.ID, Token: oldToken, RunID: first.Run.ID,
		ErrorCode: "NAVIGATION_TIMEOUT", ErrorMsg: "late",
	})
	if !errors.Is(err, store.ErrLeaseLost) {
		t.Fatalf("old failure commit err = %v, want ErrLeaseLost", err)
	}

	cur := testutil.WaitForTaskStatus(t, st, task.ID, models.StateRunning)
	if cur.Owner == nil || *cur.Owner == "worker-A" {
		t.Fatalf("task owner wrong after late commits: %+v", cur.Owner)
	}
	runs, _ := st.ListRunsByTask(context.Background(), task.ID)
	for _, r := range runs {
		if r.ID == second.Run.ID && r.Status != models.StateRunning {
			t.Fatalf("current run was disturbed: %s", r.Status)
		}
	}

	// The new executor commits successfully.
	if err := st.CommitSuccess(context.Background(), store.SuccessInput{
		TaskID: task.ID, Token: *second.Task.LeaseToken, RunID: second.Run.ID,
		FinalURL: task.URL, Collected: m, ReportMD: "fresh",
	}); err != nil {
		t.Fatalf("new commit: %v", err)
	}
	// Another success commit on the same token must not create a 2nd report.
	if err := st.CommitSuccess(context.Background(), store.SuccessInput{
		TaskID: task.ID, Token: *second.Task.LeaseToken, RunID: second.Run.ID,
		FinalURL: task.URL, Collected: m, ReportMD: "duplicate",
	}); !errors.Is(err, store.ErrLeaseLost) {
		t.Fatalf("repeat commit = %v, want ErrLeaseLost", err)
	}
	rep, err := st.GetReportByTask(context.Background(), task.ID)
	if err != nil || rep.Markdown != "fresh" {
		t.Fatalf("report not unique/fresh: %v (%+v)", err, rep)
	}
}

// TestFailure_RetryBackoffAndDead verifies retry re-queue with backoff and
// the eventual dead state when the budget is exhausted.
func TestFailure_RetryBackoffAndDead(t *testing.T) {
	st := testutil.NewStore(t)
	task, err := st.Enqueue(context.Background(), store.EnqueueRequest{
		URL: "http://demo.local/dead", Viewport: models.ViewportMobile, MaxAttempts: 2,
	})
	if err != nil {
		t.Fatal(err)
	}
	for attempt := 1; attempt <= 2; attempt++ {
		// Make this specific task immediately due: real callers wait out the
		// retry backoff, but the test does not need to sleep.
		_ = st.DB().Model(&models.Task{}).
			Where("id = ? AND status = ?", task.ID, models.StateQueued).
			Update("run_after", time.Now().UTC()).Error
		c, err := st.Claim(context.Background(), "w", time.Minute)
		if err != nil || c == nil {
			t.Fatalf("claim %d: %v", attempt, err)
		}
		err = st.CommitFailure(context.Background(), store.FailureInput{
			TaskID: c.Task.ID, Token: *c.Task.LeaseToken, RunID: c.Run.ID,
			ErrorCode: "NAVIGATION_TIMEOUT", ErrorMsg: "boom",
		})
		if err != nil {
			t.Fatalf("failure %d: %v", attempt, err)
		}
	}
	final := testutil.WaitForTaskStatus(t, st, task.ID, models.StateDead, models.StateFailed)
	if final.Status != models.StateDead || final.Attempts != 2 {
		t.Fatalf("expected dead after 2 attempts, got %s attempts=%d", final.Status, final.Attempts)
	}
	runs, _ := st.ListRunsByTask(context.Background(), task.ID)
	if len(runs) != 2 {
		t.Fatalf("expected 2 failed runs, got %d", len(runs))
	}
	for _, r := range runs {
		if r.Status != models.StateFailed || r.ErrorCode != "NAVIGATION_TIMEOUT" {
			t.Fatalf("run %d = %s/%s", r.ID, r.Status, r.ErrorCode)
		}
	}
}

// TestFailedTask_DoesNotBlockQueue enqueues a permanently failing task ahead
// of a healthy one: the second task must still be claimable independently.
func TestFailedTask_DoesNotBlockQueue(t *testing.T) {
	st := testutil.NewStore(t)
	bad := enqueueTask(t, st, "bad")
	good := enqueueTask(t, st, "good")

	c, _ := st.Claim(context.Background(), "w", time.Minute)
	if c == nil || c.Task.ID != bad.ID {
		t.Fatalf("expected first claim to be task %d, got %v", bad.ID, c)
	}
	if err := st.CommitFailure(context.Background(), store.FailureInput{
		TaskID: bad.ID, Token: *c.Task.LeaseToken, RunID: c.Run.ID,
		ErrorCode: "BROWSER_CRASH", ErrorMsg: "x",
	}); err != nil {
		t.Fatal(err)
	}
	// The failed task is parked with run_after in the future (backoff); the
	// next claim must skip straight past it to the good task.
	c2, err := st.Claim(context.Background(), "w", time.Minute)
	if err != nil || c2 == nil {
		t.Fatalf("second claim blocked by failing task: %v", err)
	}
	if c2.Task.ID != good.ID {
		t.Fatalf("expected good task %d, got %d (failing task blocked the queue)", good.ID, c2.Task.ID)
	}
}

// TestRetrySucceededTask_Conflict pins the rule: succeeded tasks cannot be
// retried (which would risk a second report); re-measuring is a new task.
func TestRetrySucceededTask_Conflict(t *testing.T) {
	st := testutil.NewStore(t)
	task := enqueueTask(t, st, "done")
	c, _ := st.Claim(context.Background(), "w", time.Minute)
	fcp := 50.0
	m := &store.RunMetrics{FCPMS: &fcp, MetricStatus: models.MetricSet{
		Navigation: models.MetricOK, FCP: models.MetricOK, LCP: models.MetricUnsupported,
		CLS: models.MetricUnsupported, LongTasks: models.MetricUnsupported, Resources: models.MetricOK,
	}}
	if err := st.CommitSuccess(context.Background(), store.SuccessInput{
		TaskID: task.ID, Token: *c.Task.LeaseToken, RunID: c.Run.ID,
		FinalURL: task.URL, Collected: m, ReportMD: "r",
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := st.RetryTask(context.Background(), task.ID); !errors.Is(err, store.ErrConflict) {
		t.Fatalf("retry succeeded task: %v, want ErrConflict", err)
	}
}

// TestHeartbeat_OnlyCurrentTokenSucceeds checks that heartbeats follow the
// token, so an old worker cannot extend a lease it no longer owns.
func TestHeartbeat_OnlyCurrentTokenSucceeds(t *testing.T) {
	st := testutil.NewStore(t)
	task := enqueueTask(t, st, "hb")
	c, _ := st.Claim(context.Background(), "w", 30*time.Millisecond)
	time.Sleep(60 * time.Millisecond)
	c2, _ := st.Claim(context.Background(), "w2", time.Minute)
	if err := st.Heartbeat(context.Background(), task.ID, *c.Task.LeaseToken, time.Minute); !errors.Is(err, store.ErrLeaseLost) {
		t.Fatalf("old token heartbeat: %v", err)
	}
	if err := st.Heartbeat(context.Background(), task.ID, *c2.Task.LeaseToken, time.Minute); err != nil {
		t.Fatalf("current token heartbeat: %v", err)
	}
}

func owner(id int) string {
	return "worker-" + string(rune('A'+id%26)) + "-" + itoa(id)
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b []byte
	for n > 0 {
		b = append([]byte{byte('0' + n%10)}, b...)
		n /= 10
	}
	return string(b)
}
