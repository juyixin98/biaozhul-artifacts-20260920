package chain

import (
	"encoding/json"
	"fmt"
	"sync"
	"testing"
	"time"

	"forensiccore/internal/models"

	"github.com/glebarez/sqlite"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
)

var fixedTime = time.Unix(1700000000, 123456789)

func testDB(t *testing.T) *gorm.DB {
	t.Helper()
	db, err := gorm.Open(sqlite.Open("file::memory:?cache=shared"), &gorm.Config{
		Logger: logger.Default.LogMode(logger.Silent),
	})
	if err != nil {
		t.Fatal(err)
	}
	sqlDB, _ := db.DB()
	sqlDB.SetMaxOpenConns(20)
	if err := db.AutoMigrate(&models.ChainEvent{}, &models.ChainLock{}); err != nil {
		t.Fatal(err)
	}
	// Unique in-memory DB per test by dropping existing chain rows.
	db.Exec("DELETE FROM chain_events")
	db.Exec("DELETE FROM chain_locks")
	return db
}

func TestAppendAndVerifyHappyPath(t *testing.T) {
	db := testDB(t)
	for i := 0; i < 5; i++ {
		ev, err := Append(db, AppendOptions{
			CaseID: 1, Type: models.EventNote, Actor: "alice",
			Payload: map[string]any{"n": i},
		})
		if err != nil {
			t.Fatalf("append %d: %v", i, err)
		}
		if ev.Seq != i+1 {
			t.Fatalf("seq = %d, want %d", ev.Seq, i+1)
		}
	}
	res, err := Verify(db, 1)
	if err != nil {
		t.Fatal(err)
	}
	if !res.OK {
		t.Fatalf("verify faults: %+v", res.Faults)
	}
	if res.EventCount != 5 {
		t.Fatalf("count = %d", res.EventCount)
	}
}

func TestConcurrentAppendNeverForks(t *testing.T) {
	db := testDB(t)
	const n = 40
	var wg sync.WaitGroup
	errs := make(chan error, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_, err := Append(db, AppendOptions{
				CaseID: 7, Type: models.EventNote, Actor: fmt.Sprintf("actor-%d", i),
				Payload: map[string]any{"i": i},
			})
			errs <- err
		}(i)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatalf("concurrent append: %v", err)
		}
	}

	var events []models.ChainEvent
	if err := db.Where("case_id = ?", 7).Order("seq ASC").Find(&events).Error; err != nil {
		t.Fatal(err)
	}
	if len(events) != n {
		t.Fatalf("event count = %d, want %d", len(events), n)
	}
	// Every seq must be present exactly once and every prev must link to the
	// actual predecessor entry digest (no fork, no gap).
	prev := Genesis
	seen := map[int]bool{}
	for _, e := range events {
		if seen[e.Seq] {
			t.Fatalf("duplicate seq %d", e.Seq)
		}
		seen[e.Seq] = true
		if e.PrevDigest != prev {
			t.Fatalf("seq %d forks: prev %s != expected %s", e.Seq, e.PrevDigest, prev)
		}
		prev = e.EntryDigest
	}
	for i := 1; i <= n; i++ {
		if !seen[i] {
			t.Fatalf("missing seq %d", i)
		}
	}
	res, err := Verify(db, 7)
	if err != nil {
		t.Fatal(err)
	}
	if !res.OK {
		t.Fatalf("verify faults after concurrent appends: %+v", res.Faults)
	}
}

func TestTamperedPayloadDetected(t *testing.T) {
	db := testDB(t)
	for i := 0; i < 4; i++ {
		_, err := Append(db, AppendOptions{CaseID: 3, Type: models.EventNote, Actor: "a",
			Payload: map[string]any{"body": fmt.Sprintf("note %d", i)}})
		if err != nil {
			t.Fatal(err)
		}
	}
	// Tamper with the stored payload of seq 2 (simulating a row-level edit).
	if err := db.Exec(`UPDATE chain_events SET payload = ? WHERE case_id = 3 AND seq = 2`,
		mustJSON(map[string]any{"body": "forged"})).Error; err != nil {
		t.Fatal(err)
	}
	res, err := Verify(db, 3)
	if err != nil {
		t.Fatal(err)
	}
	if res.OK {
		t.Fatal("expected verification failure")
	}
	var found bool
	for _, f := range res.Faults {
		if f.Seq == 2 && f.Code == FaultTampered {
			found = true
		}
	}
	if !found {
		t.Fatalf("expected TAMPERED fault at seq 2, got %+v", res.Faults)
	}
}

func TestTamperedDigestDetected(t *testing.T) {
	db := testDB(t)
	ev, _ := Append(db, AppendOptions{CaseID: 4, Type: models.EventNote, Actor: "a",
		Payload: map[string]any{"x": 1}})
	_, _ = Append(db, AppendOptions{CaseID: 4, Type: models.EventNote, Actor: "a",
		Payload: map[string]any{"x": 2}})
	// Corrupt the entry digest of the first event.
	db.Exec(`UPDATE chain_events SET entry_digest = ? WHERE id = ?`,
		"0000000000000000000000000000000000000000000000000000000000000000", ev.ID)
	res, _ := Verify(db, 4)
	if res.OK || len(res.Faults) == 0 {
		t.Fatalf("expected faults, got %+v", res)
	}
}

func TestMissingEventDetected(t *testing.T) {
	db := testDB(t)
	for i := 0; i < 5; i++ {
		_, _ = Append(db, AppendOptions{CaseID: 5, Type: models.EventNote, Actor: "a",
			Payload: map[string]any{"i": i}})
	}
	// Delete seq 3: gap + broken prev link at old seq 4.
	db.Exec(`DELETE FROM chain_events WHERE case_id = 5 AND seq = 3`)
	res, _ := Verify(db, 5)
	if res.OK {
		t.Fatal("expected failure for missing event")
	}
	var missing, tampered bool
	for _, f := range res.Faults {
		if f.Code == FaultMissing {
			missing = true
		}
		if f.Code == FaultTampered {
			tampered = true
		}
	}
	if !missing || !tampered {
		t.Fatalf("expected MISSING and TAMPERED faults, got %+v", res.Faults)
	}
}

func TestReorderedEventsDetected(t *testing.T) {
	db := testDB(t)
	for i := 0; i < 4; i++ {
		_, _ = Append(db, AppendOptions{CaseID: 6, Type: models.EventNote, Actor: "a",
			Payload: map[string]any{"i": i}})
	}
	// Swap seq values between rows 2 and 3 (drop unique index temporarily).
	db.Exec(`DROP INDEX IF EXISTS idx_case_seq`)
	db.Exec(`UPDATE chain_events SET seq = 99 WHERE case_id = 6 AND seq = 2`)
	db.Exec(`UPDATE chain_events SET seq = 2 WHERE case_id = 6 AND seq = 3`)
	db.Exec(`UPDATE chain_events SET seq = 3 WHERE case_id = 6 AND seq = 99`)

	res, _ := Verify(db, 6)
	if res.OK {
		t.Fatal("expected failure for reordered events")
	}
	// Position 3 (old seq 3 carrying prev digest for seq 2) can no longer
	// recompute consistently: at least one TAMPERED/OUT_OF_ORDER fault.
	var found bool
	for _, f := range res.Faults {
		if f.Code == FaultTampered || f.Code == FaultOutOfOrder {
			found = true
		}
	}
	if !found {
		t.Fatalf("expected tamper/order faults, got %+v", res.Faults)
	}
}

func TestCanonicalDigestStable(t *testing.T) {
	// Key order in the payload map must not change the content digest.
	p1 := mustJSON(map[string]any{"b": 2, "a": 1})
	p2 := mustJSON(map[string]any{"a": 1, "b": 2})
	d1, _, err := ComputeDigests(1, models.EventNote, "a", p1, Genesis, fixedTime)
	if err != nil {
		t.Fatal(err)
	}
	d2, _, err := ComputeDigests(1, models.EventNote, "a", p2, Genesis, fixedTime)
	if err != nil {
		t.Fatal(err)
	}
	if d1 != d2 {
		t.Fatal("canonical digest must be independent of JSON key order")
	}
}

func mustJSON(v any) []byte {
	b, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	return b
}
