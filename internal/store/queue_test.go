package store

import (
	"context"
	"fmt"
	"os"
	"sync"
	"testing"
	"time"

	"gorm.io/gorm"

	"sitevitals/internal/models"
)

// testDB opens a connection to the local MySQL test schema and migrates it.
// Tests skip when the database is unreachable.
func testDB(t *testing.T) *gorm.DB {
	t.Helper()
	dsn := os.Getenv("SV_TEST_DSN")
	if dsn == "" {
		dsn = "root:@tcp(127.0.0.1:3306)/sitevitals_b_store_test?charset=utf8mb4&parseTime=True&loc=UTC&multiStatements=true"
	}
	db, err := Open(dsn)
	if err != nil {
		t.Skipf("mysql not available: %v", err)
	}
	if err := AutoMigrate(db); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	// Clean every table for deterministic ordering.
	for _, m := range models.AllModels() {
		stmt := &gorm.Statement{DB: db}
		if err := stmt.Parse(m); err != nil {
			t.Fatal(err)
		}
		if err := db.Exec("DELETE FROM " + stmt.Schema.Table).Error; err != nil {
			t.Fatalf("cleanup: %v", err)
		}
	}
	t.Cleanup(func() {
		sqlDB, _ := db.DB()
		_ = sqlDB.Close()
	})
	return db
}

func TestClaimAndFencing(t *testing.T) {
	db := testDB(t)
	ctx := context.Background()
	q := NewQueue(db)

	const n = 5
	for i := 0; i < n; i++ {
		if _, err := q.Enqueue(ctx, "http://x.test/p", models.ViewportDesktop, 3); err != nil {
			t.Fatal(err)
		}
	}

	// Concurrent claimers must each receive a distinct job (SKIP LOCKED).
	seen := map[uint64]int{}
	claims := make(chan *Claim, n)
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			c, err := q.ClaimJob(ctx, fmt.Sprintf("h-%d", i), time.Minute)
			if err != nil {
				t.Errorf("claim: %v", err)
				return
			}
			claims <- c
		}()
	}
	wg.Wait()
	close(claims)
	for c := range claims {
		seen[c.Job.ID]++
		if c.Run.FencingToken != 1 || c.Run.Attempt != 1 {
			t.Errorf("job %d first claim: fence=%d attempt=%d want 1/1", c.Job.ID, c.Run.FencingToken, c.Run.Attempt)
		}
	}
	if len(seen) != n {
		t.Fatalf("distinct claims=%d want %d: %v", len(seen), n, seen)
	}

	if _, err := q.ClaimJob(ctx, "h", time.Minute); err != ErrNoJob {
		t.Fatalf("empty queue err=%v want ErrNoJob", err)
	}
}

func TestStaleSuccessCannotOverwrite(t *testing.T) {
	db := testDB(t)
	ctx := context.Background()
	q := NewQueue(db)
	job, _ := q.Enqueue(ctx, "http://x.test/p", models.ViewportDesktop, 3)

	// First (later "crashed") worker claims with a short lease.
	first, err := q.ClaimJob(ctx, "old-holder", 20*time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	time.Sleep(30 * time.Millisecond)

	// Lease expires; reaper recovers the job.
	if n, err := q.ReapExpired(ctx, time.Now().UTC()); err != nil || n != 1 {
		t.Fatalf("reap n=%d err=%v want 1", n, err)
	}

	// A stale heartbeat from the old worker must be rejected.
	if err := q.Heartbeat(ctx, job.ID, first.Run.FencingToken, "old-holder", time.Minute); err != ErrLeaseLost {
		t.Fatalf("stale heartbeat err=%v want ErrLeaseLost", err)
	}

	// New worker claims: fence 2.
	second, err := q.ClaimJob(ctx, "new-holder", time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if second.Run.FencingToken != 2 {
		t.Fatalf("second fence=%d want 2", second.Run.FencingToken)
	}

	// Old worker submits late: must be discarded.
	ok, err := q.ReportSuccess(ctx, first.Job.ID, first.Run.ID, first.Run.FencingToken, "old-holder",
		&SuccessReport{FinalURL: "http://x.test/p", Metrics: []*models.Metric{
			{Name: "fcp_ms", Status: models.MetricCollected, ValueMS: ptr(1)},
		}})
	if err != nil {
		t.Fatal(err)
	}
	if ok {
		t.Fatal("late success report from old fence must be rejected")
	}

	// New worker submits: accepted.
	ok, err = q.ReportSuccess(ctx, second.Job.ID, second.Run.ID, second.Run.FencingToken, "new-holder",
		&SuccessReport{FinalURL: "http://x.test/p", Metrics: []*models.Metric{
			{Name: "fcp_ms", Status: models.MetricCollected, ValueMS: ptr(100)},
		}})
	if err != nil || !ok {
		t.Fatalf("new success ok=%v err=%v", ok, err)
	}

	// A second success (duplicate report for the same job) must not land.
	ok, err = q.ReportSuccess(ctx, second.Job.ID, second.Run.ID, second.Run.FencingToken, "new-holder",
		&SuccessReport{Metrics: []*models.Metric{{Name: "fcp_ms", Status: models.MetricCollected, ValueMS: ptr(2)}}})
	if err != nil {
		t.Fatal(err)
	}
	if ok {
		t.Fatal("duplicate success report must be rejected")
	}

	var final models.Job
	if err := db.Take(&final, job.ID).Error; err != nil {
		t.Fatal(err)
	}
	if final.Status != models.JobSucceeded || final.SucceededRunID == nil || *final.SucceededRunID != second.Run.ID {
		t.Fatalf("job final state wrong: %+v", final)
	}

	var metricCount int64
	db.Model(&models.Metric{}).Where("run_id = ?", first.Run.ID).Count(&metricCount)
	if metricCount != 0 {
		t.Fatalf("old run got %d metric rows; late writes must not persist", metricCount)
	}
	var metricCount2 int64
	db.Model(&models.Metric{}).Where("run_id = ?", second.Run.ID).Count(&metricCount2)
	if metricCount2 != 1 {
		t.Fatalf("new run metric rows=%d want 1", metricCount2)
	}
}

func TestFailureRetryThenSingleReport(t *testing.T) {
	db := testDB(t)
	ctx := context.Background()
	q := NewQueue(db)
	job, _ := q.Enqueue(ctx, "http://x.test/p", models.ViewportMobile, 2)

	first, err := q.ClaimJob(ctx, "h", time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	ok, err := q.ReportFailure(ctx, job.ID, first.Run.ID, 1, "h", models.FailBrowserExited, "crashed")
	if err != nil || !ok {
		t.Fatalf("first failure ok=%v err=%v", ok, err)
	}
	var j models.Job
	db.Take(&j, job.ID)
	if j.Status != models.JobQueued || j.Attempts != 1 {
		t.Fatalf("after failure status=%s attempts=%d want queued/1", j.Status, j.Attempts)
	}

	second, err := q.ClaimJob(ctx, "h", time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if second.Run.FencingToken != 2 || second.Run.Attempt != 2 {
		t.Fatalf("retry fence=%d attempt=%d want 2/2", second.Run.FencingToken, second.Run.Attempt)
	}
	ok, _ = q.ReportSuccess(ctx, job.ID, second.Run.ID, 2, "h",
		&SuccessReport{Metrics: []*models.Metric{{Name: "fcp_ms", Status: models.MetricCollected, ValueMS: ptr(5)}}})
	if !ok {
		t.Fatal("retry success must be accepted")
	}

	// Exactly one succeeded run and one set of metric rows.
	var succ int64
	db.Model(&models.Run{}).Where("job_id = ? AND status = ?", job.ID, models.RunSucceeded).Count(&succ)
	if succ != 1 {
		t.Fatalf("succeeded runs=%d want 1", succ)
	}
	var metrics int64
	db.Model(&models.Metric{}).
		Joins("JOIN runs ON runs.id = metrics.run_id").
		Where("runs.job_id = ?", job.ID).Count(&metrics)
	if metrics != 1 {
		t.Fatalf("total metric rows=%d want 1 (no duplicate report)", metrics)
	}
}

func TestAttemptsExhausted(t *testing.T) {
	db := testDB(t)
	ctx := context.Background()
	q := NewQueue(db)
	job, _ := q.Enqueue(ctx, "http://x.test/p", models.ViewportMobile, 1)
	c, _ := q.ClaimJob(ctx, "h", time.Minute)
	ok, err := q.ReportFailure(ctx, job.ID, c.Run.ID, 1, "h", models.FailNavigationTimeout, "timed out")
	if err != nil || !ok {
		t.Fatalf("failure ok=%v err=%v", ok, err)
	}
	var j models.Job
	db.Take(&j, job.ID)
	if j.Status != models.JobFailed {
		t.Fatalf("status=%s want failed", j.Status)
	}
	if _, err := q.ClaimJob(ctx, "h2", time.Minute); err != ErrNoJob {
		t.Fatalf("terminal job must not be claimable, err=%v", err)
	}
}

func ptr(v float64) *float64 { return &v }
