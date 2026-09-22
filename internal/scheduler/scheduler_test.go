package scheduler_test

import (
	"context"
	"strconv"
	"sync"
	"testing"
	"time"

	"anomalywatch/internal/detection"
	"anomalywatch/internal/models"
	"anomalywatch/internal/testdb"
)

// Duplicated scheduler ticks/replicas must not double-escalate: the MySQL
// named lock serializes jobs, and escalation itself only touches status='new'.
func TestDuplicateSweepRunsAreSafe(t *testing.T) {
	gdb, cfg := testdb.New(t)
	eng := detection.New(gdb, cfg)

	emp := testdb.EmployeeIDs(t, gdb)["Alice Chen"]
	old := time.Now().UTC().Add(-25 * time.Hour)
	if err := gdb.Create(&models.Alert{
		RuleCode: models.RuleFirstUSB, RuleVersion: 1, EmployeeID: emp,
		Status: models.AlertStatusNew, Severity: "high", Title: "old",
		DedupKey: "usb:dup-sched", Evidence: models.JSONMap{},
		FiredAt: old, UpdatedAt: old,
	}).Error; err != nil {
		t.Fatal(err)
	}

	// Two sweeps run "concurrently" through the lock; only one can hold it.
	var wg sync.WaitGroup
	results := make([]int, 2)
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			ran, err := eng.WithJobLock(context.Background(), "job:escalate-test", func() error {
				n, err := eng.EscalateOverdue()
				results[i] = n
				return err
			})
			if err != nil {
				t.Errorf("run %d: %v", i, err)
			}
			if !ran {
				// If the other goroutine holds the lock for the whole window,
				// that's also acceptable: no double work happened.
				results[i] = -1
			}
		}(i)
	}
	wg.Wait()

	escalated := 0
	for _, r := range results {
		if r > 0 {
			escalated += r
		}
	}
	if escalated != 1 {
		t.Fatalf("total escalated across duplicate runs = %d, want exactly 1", escalated)
	}

	// Immediate duplicate tick is a no-op.
	n, err := eng.EscalateOverdue()
	if err != nil || n != 0 {
		t.Fatalf("follow-up escalate = (%d,%v), want (0,nil)", n, err)
	}
}

// Re-entrant behavior across sequential process passes must not reprocess
// already-claimed events.
func TestProcessClaimsAreExclusive(t *testing.T) {
	gdb, cfg := testdb.New(t)
	eng := detection.New(gdb, cfg)
	emp := testdb.EmployeeIDs(t, gdb)["David Zhao"]
	base := time.Date(2026, 9, 20, 12, 0, 0, 0, time.UTC)
	for i := 0; i < 5; i++ {
		if err := gdb.Exec(`INSERT INTO events
			(source,event_id,employee_id,event_type,occurred_at,metadata,content_hash,ingest_token,processed,received_at)
			VALUES ('s',?,?,'login',?,CAST('{}' AS JSON),'h','',0,UTC_TIMESTAMP(3))`,
			"claim-"+strconv.Itoa(i), emp, base.Add(time.Duration(i)*time.Minute)).Error; err != nil {
			t.Fatal(err)
		}
	}
	n1, err := eng.ProcessPending(3)
	if err != nil || n1 != 3 {
		t.Fatalf("first pass = (%d,%v), want 3", n1, err)
	}
	n2, err := eng.ProcessPending(3)
	if err != nil || n2 != 2 {
		t.Fatalf("second pass = (%d,%v), want remaining 2", n2, err)
	}
	n3, err := eng.ProcessPending(3)
	if err != nil || n3 != 0 {
		t.Fatalf("third pass = (%d,%v), want 0", n3, err)
	}
}
