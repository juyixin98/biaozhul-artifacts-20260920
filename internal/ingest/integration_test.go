package ingest_test

import (
	"errors"
	"sync"
	"testing"
	"time"

	"anomalywatch/internal/config"
	"anomalywatch/internal/ingest"
	"anomalywatch/internal/models"
	"anomalywatch/internal/testdb"

	"gorm.io/gorm"
)

type fixture struct {
	db  *gorm.DB
	cfg config.Config
	svc *ingest.Service
	now time.Time
}

func setup(t *testing.T) fixture {
	gdb, cfg := testdb.New(t)
	cfg.MaxBackfillAge = 90 * 24 * time.Hour
	now := time.Date(2026, 9, 21, 10, 0, 0, 0, time.UTC)
	return fixture{db: gdb, cfg: cfg, svc: ingest.New(gdb, cfg), now: now}
}

func loginEvent(id string, emp uint64, at time.Time) ingest.EventIn {
	return ingest.EventIn{EventID: id, EmployeeID: emp, EventType: models.EventTypeLogin, OccurredAt: at}
}

func TestIngestInsertsAndIdempotentDuplicate(t *testing.T) {
	f := setup(t)
	emps := testdb.EmployeeIDs(t, f.db)
	req := &ingest.BatchRequest{Source: "s1", Events: []ingest.EventIn{
		loginEvent("e1", emps["Alice Chen"], f.now.Add(-time.Hour)),
	}}
	res, err := f.svc.Ingest(req, f.now)
	if err != nil {
		t.Fatalf("first ingest: %v", err)
	}
	if res.Inserted != 1 {
		t.Fatalf("inserted = %d, want 1", res.Inserted)
	}

	// Exact duplicate report: accepted, not booked twice.
	res2, err := f.svc.Ingest(req, f.now.Add(time.Minute))
	if err != nil {
		t.Fatalf("duplicate ingest: %v", err)
	}
	if res2.Duplicate != 1 || res2.Inserted != 0 {
		t.Fatalf("duplicate = %d inserted = %d, want 1/0", res2.Duplicate, res2.Inserted)
	}
	var n int64
	f.db.Model(&models.Event{}).Where("event_id = ?", "e1").Count(&n)
	if n != 1 {
		t.Fatalf("stored rows = %d, want 1", n)
	}
}

func TestIngestSameIDDifferentContentConflict(t *testing.T) {
	f := setup(t)
	emps := testdb.EmployeeIDs(t, f.db)
	first := &ingest.BatchRequest{Source: "s1", Events: []ingest.EventIn{
		loginEvent("e1", emps["Alice Chen"], f.now.Add(-time.Hour)),
	}}
	if _, err := f.svc.Ingest(first, f.now); err != nil {
		t.Fatalf("first: %v", err)
	}
	// Same source+id, different occurred_at -> 409 conflict, must not overwrite.
	conflict := &ingest.BatchRequest{Source: "s1", Events: []ingest.EventIn{
		loginEvent("e1", emps["Alice Chen"], f.now.Add(-2*time.Hour)),
	}}
	_, err := f.svc.Ingest(conflict, f.now.Add(time.Minute))
	var ce *ingest.ConflictError
	if !errors.As(err, &ce) {
		t.Fatalf("want ConflictError, got %v", err)
	}
	if ce.Result.Conflict != 1 {
		t.Fatalf("conflicts = %d, want 1", ce.Result.Conflict)
	}
	var stored models.Event
	if err := f.db.Where("source = ? AND event_id = ?", "s1", "e1").First(&stored).Error; err != nil {
		t.Fatalf("reload: %v", err)
	}
	if !stored.OccurredAt.Equal(f.now.Add(-time.Hour)) {
		t.Fatalf("stored occurred_at = %s, original must be preserved", stored.OccurredAt)
	}
}

func TestIngestMixedBatchConflictAndInsert(t *testing.T) {
	f := setup(t)
	emps := testdb.EmployeeIDs(t, f.db)
	_, err := f.svc.Ingest(&ingest.BatchRequest{Source: "s1", Events: []ingest.EventIn{
		loginEvent("dup", emps["Alice Chen"], f.now.Add(-time.Hour)),
	}}, f.now)
	if err != nil {
		t.Fatal(err)
	}
	res, err := f.svc.Ingest(&ingest.BatchRequest{Source: "s1", Events: []ingest.EventIn{
		loginEvent("dup", emps["Alice Chen"], f.now.Add(-2*time.Hour)), // conflict
		loginEvent("new", emps["Bob Li"], f.now.Add(-time.Hour)),       // still committed
	}}, f.now.Add(time.Minute))
	var ce *ingest.ConflictError
	if !errors.As(err, &ce) {
		t.Fatalf("want ConflictError, got %v", err)
	}
	if ce.Result.Conflict != 1 || res.Inserted != 1 {
		t.Fatalf("conflict = %d inserted = %d, want 1/1", ce.Result.Conflict, res.Inserted)
	}
}

func TestIngestRejectsOversizedBatch(t *testing.T) {
	f := setup(t)
	f.cfg.MaxEventsPerBatch = 3
	svc := ingest.New(f.db, f.cfg) // service captures cfg by value
	emps := testdb.EmployeeIDs(t, f.db)
	var evs []ingest.EventIn
	for i := 0; i < 4; i++ {
		evs = append(evs, loginEvent("e"+itoa(i), emps["Alice Chen"], f.now.Add(-time.Hour)))
	}
	_, err := svc.Ingest(&ingest.BatchRequest{Events: evs}, f.now)
	var re *ingest.RejectError
	if !errors.As(err, &re) {
		t.Fatalf("want RejectError, got %v", err)
	}
}

func TestIngestRejectsOutsideBackfillWindow(t *testing.T) {
	f := setup(t)
	emps := testdb.EmployeeIDs(t, f.db)
	tooOld := &ingest.BatchRequest{Events: []ingest.EventIn{
		loginEvent("old", emps["Alice Chen"], f.now.Add(-f.cfg.MaxBackfillAge-time.Minute)),
	}}
	if _, err := f.svc.Ingest(tooOld, f.now); err == nil {
		t.Fatal("event older than backfill horizon must be rejected")
	}
	tooNew := &ingest.BatchRequest{Events: []ingest.EventIn{
		loginEvent("future", emps["Alice Chen"], f.now.Add(f.cfg.MaxFutureDelay+time.Minute)),
	}}
	if _, err := f.svc.Ingest(tooNew, f.now); err == nil {
		t.Fatal("event too far in the future must be rejected")
	}
}

func TestIngestRejectsUnknownEmployeeAndBadType(t *testing.T) {
	f := setup(t)
	badEmp := &ingest.BatchRequest{Events: []ingest.EventIn{
		loginEvent("x", 999999, f.now),
	}}
	if _, err := f.svc.Ingest(badEmp, f.now); err == nil {
		t.Fatal("unknown employee must reject the batch")
	}
	badType := &ingest.BatchRequest{Events: []ingest.EventIn{{
		EventID: "y", EmployeeID: 1, EventType: "print", OccurredAt: f.now,
	}}}
	if _, err := f.svc.Ingest(badType, f.now); err == nil {
		t.Fatal("unknown event type must reject the batch")
	}
}

func TestIngestLateOutOfOrderAccepted(t *testing.T) {
	f := setup(t)
	emps := testdb.EmployeeIDs(t, f.db)
	// Submit a newer event first, then an older one in the same window.
	_, err := f.svc.Ingest(&ingest.BatchRequest{Source: "s1", Events: []ingest.EventIn{
		loginEvent("later", emps["Alice Chen"], f.now.Add(-time.Hour)),
	}}, f.now)
	if err != nil {
		t.Fatal(err)
	}
	res, err := f.svc.Ingest(&ingest.BatchRequest{Source: "s1", Events: []ingest.EventIn{
		loginEvent("earlier", emps["Alice Chen"], f.now.Add(-48*time.Hour)),
	}}, f.now.Add(time.Minute))
	if err != nil {
		t.Fatalf("late in-window event: %v", err)
	}
	if res.Inserted != 1 {
		t.Fatalf("inserted = %d, want 1", res.Inserted)
	}
}

// Regression: receive timestamps carry nanoseconds but MySQL DATETIME(3)
// truncates to milliseconds. The post-insert classification must still report
// a fresh row as inserted rather than duplicate.
func TestIngestSubMillisecondNowReportsInserted(t *testing.T) {
	f := setup(t)
	emps := testdb.EmployeeIDs(t, f.db)
	now := time.Date(2026, 9, 21, 10, 0, 0, 123456789, time.UTC) // 123.456789 ms
	res, err := f.svc.Ingest(&ingest.BatchRequest{Source: "ns", Events: []ingest.EventIn{
		loginEvent("ns-1", emps["Alice Chen"], now.Add(-time.Hour)),
	}}, now)
	if err != nil {
		t.Fatal(err)
	}
	if res.Inserted != 1 {
		t.Fatalf("inserted = %d, want 1 (got duplicate=%d)", res.Inserted, res.Duplicate)
	}
}

// TestIngestConcurrentIdenticalBatches fires the same batch from many goroutines.
// Exactly one row may exist afterwards and every response must be inserted or
// duplicate (never a false conflict).
func TestIngestConcurrentIdenticalBatches(t *testing.T) {
	f := setup(t)
	emps := testdb.EmployeeIDs(t, f.db)
	req := &ingest.BatchRequest{Source: "conc", Events: []ingest.EventIn{
		loginEvent("same-id", emps["Alice Chen"], f.now.Add(-time.Hour)),
	}}
	const workers = 12
	var wg sync.WaitGroup
	errs := make(chan error, workers)
	conflicts := make(chan int, workers)
	ins := make(chan int, workers)
	dups := make(chan int, workers)
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			// Same receive instant (down to nanoseconds) for every worker to
			// stress the millisecond-truncation classification.
			res, err := f.svc.Ingest(req, f.now)
			if err != nil {
				var ce *ingest.ConflictError
				if errors.As(err, &ce) {
					conflicts <- ce.Result.Conflict
					return
				}
				errs <- err
				return
			}
			ins <- res.Inserted
			dups <- res.Duplicate
			if res.Conflict > 0 {
				conflicts <- res.Conflict
			}
		}()
	}
	wg.Wait()
	close(errs)
	close(conflicts)
	close(ins)
	close(dups)
	for err := range errs {
		t.Fatalf("concurrent ingest: %v", err)
	}
	for c := range conflicts {
		if c > 0 {
			t.Fatal("identical concurrent batches must not be reported as conflicts")
		}
	}
	totalIns, totalDups := 0, 0
	for n := range ins {
		totalIns += n
	}
	for n := range dups {
		totalDups += n
	}
	if totalIns != 1 || totalDups != workers-1 {
		t.Fatalf("across %d workers: inserted=%d (want 1), duplicate=%d (want %d)",
			workers, totalIns, totalDups, workers-1)
	}
	var n int64
	f.db.Model(&models.Event{}).Where("source = ? AND event_id = ?", "conc", "same-id").Count(&n)
	if n != 1 {
		t.Fatalf("stored rows = %d, want exactly 1", n)
	}
}

// TestIngestConcurrentDisjointBatches checks deadlock-free high concurrency.
func TestIngestConcurrentDisjointBatches(t *testing.T) {
	f := setup(t)
	emps := testdb.EmployeeIDs(t, f.db)
	const workers = 8
	const perWorker = 50
	var wg sync.WaitGroup
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			evs := make([]ingest.EventIn, 0, perWorker)
			for i := 0; i < perWorker; i++ {
				id := "w" + itoa(w) + "-" + itoa(i)
				evs = append(evs, loginEvent(id, emps["Alice Chen"], f.now.Add(-time.Duration(i)*time.Minute)))
			}
			// Split into smaller batches to respect the 2000 cap trivially.
			for start := 0; start < len(evs); start += 25 {
				end := start + 25
				if end > len(evs) {
					end = len(evs)
				}
				if _, err := f.svc.Ingest(&ingest.BatchRequest{Source: "disjoint", Events: evs[start:end]}, f.now); err != nil {
					t.Errorf("worker %d: %v", w, err)
					return
				}
			}
		}(w)
	}
	wg.Wait()
	var n int64
	f.db.Model(&models.Event{}).Where("source = ?", "disjoint").Count(&n)
	if n != workers*perWorker {
		t.Fatalf("stored = %d, want %d", n, workers*perWorker)
	}
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	var b [12]byte
	pos := len(b)
	for i > 0 {
		pos--
		b[pos] = byte('0' + i%10)
		i /= 10
	}
	return string(b[pos:])
}
