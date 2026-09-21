package chain

import (
	"context"
	"fmt"
	"sync"
	"testing"

	"forensiccore/internal/domain"
	"forensiccore/internal/hashing"

	"gorm.io/driver/sqlite"
	"gorm.io/gorm"
)

func newTestDB(t *testing.T) *gorm.DB {
	t.Helper()
	dsn := fmt.Sprintf("file:%s?mode=memory&cache=shared", t.Name())
	db, err := gorm.Open(sqlite.Open(dsn), &gorm.Config{})
	if err != nil {
		t.Fatalf("open sqlite: %v", err)
	}
	if err := db.AutoMigrate(domain.AllModels()...); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	sqlDB, _ := db.DB()
	sqlDB.SetMaxOpenConns(1)
	t.Cleanup(func() {
		sqlDB, _ := db.DB()
		_ = sqlDB.Close()
	})
	return db
}

func seedCase(t *testing.T, db *gorm.DB, id string) {
	t.Helper()
	kase := domain.Case{ID: id, Name: "c", GenesisHash: hashing.GenesisHash(id)}
	if err := db.Create(&kase).Error; err != nil {
		t.Fatalf("seed case: %v", err)
	}
}

type note struct {
	N int `json:"n"`
}

// TestConcurrentAppendNoFork fires many goroutines at one case and requires a
// gap-free sequence 1..N with every link digest valid.
func TestConcurrentAppendNoFork(t *testing.T) {
	db := newTestDB(t)
	caseID := "case-fork"
	seedCase(t, db, caseID)
	app := New(db)

	const n = 60
	var wg sync.WaitGroup
	errs := make(chan error, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_, err := app.Append(context.Background(), AppendInput{
				CaseID:    caseID,
				EventType: domain.EventNote,
				Actor:     "role:analyst",
				Payload:   note{N: i},
			})
			if err != nil {
				errs <- err
			}
		}(i)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatalf("append failed: %v", err)
	}

	rep, err := app.Verify(context.Background(), caseID)
	if err != nil {
		t.Fatalf("verify: %v", err)
	}
	if !rep.Intact {
		t.Fatalf("chain not intact after concurrent appends: %+v", rep.Issues)
	}
	if rep.HeadSeq != n {
		t.Fatalf("expected head seq %d, got %d (an event was lost)", n, rep.HeadSeq)
	}
	if rep.EventCount != n {
		t.Fatalf("expected %d events, got %d", n, rep.EventCount)
	}
}

func TestVerifyDetectsTamperingGapAndReorder(t *testing.T) {
	ctx := context.Background()
	db := newTestDB(t)
	caseID := "case-tamper"
	seedCase(t, db, caseID)
	app := New(db)

	for i := 0; i < 5; i++ {
		if _, err := app.Append(ctx, AppendInput{
			CaseID: caseID, EventType: domain.EventNote, Actor: "a",
			Payload: note{N: i},
		}); err != nil {
			t.Fatalf("append %d: %v", i, err)
		}
	}

	// Intact baseline.
	rep, err := app.Verify(ctx, caseID)
	if err != nil {
		t.Fatal(err)
	}
	if !rep.Intact {
		t.Fatalf("expected intact chain: %+v", rep.Issues)
	}

	t.Run("content tampered", func(t *testing.T) {
		var ev3 domain.ChainEvent
		if err := db.First(&ev3, "case_id = ? AND seq = ?", caseID, 3).Error; err != nil {
			t.Fatal(err)
		}
		db.Exec("UPDATE chain_events SET content_json = ? WHERE id = ?",
			`{"v":1,"case_id":"case-tamper","event_type":"note","actor":"a","ts":"2026-01-01T00:00:00Z","payload":{"n":999}}`,
			ev3.ID)
		rep, err := app.Verify(ctx, caseID)
		if err != nil {
			t.Fatal(err)
		}
		if rep.Intact {
			t.Fatal("expected tampering to be detected")
		}
		found := false
		for _, is := range rep.Issues {
			if is.Code == IssueContentDigest {
				found = true
			}
		}
		if !found {
			t.Fatalf("expected %s issue, got %+v", IssueContentDigest, rep.Issues)
		}
		// Restore.
		db.Exec("UPDATE chain_events SET content_json = ? WHERE id = ?", ev3.ContentJSON, ev3.ID)
	})

	t.Run("event deleted (gap)", func(t *testing.T) {
		db.Exec("DELETE FROM chain_events WHERE case_id = ? AND seq = ?", caseID, 4)
		rep, err := app.Verify(ctx, caseID)
		if err != nil {
			t.Fatal(err)
		}
		if rep.Intact {
			t.Fatal("expected gap to be detected")
		}
		found := false
		for _, is := range rep.Issues {
			if is.Code == IssueGap || is.Code == IssueBrokenLink {
				found = true
			}
		}
		if !found {
			t.Fatalf("expected gap/broken-link issue, got %+v", rep.Issues)
		}
	})

	t.Run("prev link rewritten (reorder/replace)", func(t *testing.T) {
		// Point seq 2's prev_digest at the seq 1 digest of a different chain:
		// build a second case and steal its link target.
		other := "case-other"
		seedCase(t, db, other)
		if _, err := app.Append(ctx, AppendInput{
			CaseID: other, EventType: domain.EventNote, Actor: "a", Payload: note{N: 0},
		}); err != nil {
			t.Fatal(err)
		}
		var otherEv1 domain.ChainEvent
		if err := db.First(&otherEv1, "case_id = ? AND seq = 1", other).Error; err != nil {
			t.Fatal(err)
		}
		db.Exec("UPDATE chain_events SET prev_digest = ? WHERE case_id = ? AND seq = ?",
			otherEv1.Digest, caseID, 2)
		rep, err := app.Verify(ctx, caseID)
		if err != nil {
			t.Fatal(err)
		}
		found := false
		for _, is := range rep.Issues {
			if is.Code == IssueBrokenLink {
				found = true
			}
		}
		if !found {
			t.Fatalf("expected broken-link detection, got %+v", rep.Issues)
		}
	})
}

func TestGenesisAnchor(t *testing.T) {
	db := newTestDB(t)
	caseID := "case-gen"
	seedCase(t, db, caseID)
	app := New(db)
	ev, err := app.Append(context.Background(), AppendInput{
		CaseID: caseID, EventType: domain.EventNote, Actor: "a", Payload: note{N: 1},
	})
	if err != nil {
		t.Fatal(err)
	}
	if ev.Seq != 1 {
		t.Fatalf("first seq = %d, want 1", ev.Seq)
	}
	if ev.PrevDigest != hashing.GenesisHash(caseID) {
		t.Fatal("first event must anchor at the case genesis hash")
	}
	want := hashing.LinkDigest(ev.PrevDigest, ev.ContentJSON)
	if ev.Digest != want {
		t.Fatal("digest link mismatch")
	}
}

// TestConcurrentMultipleCasesIsolation makes sure locks do not serialize
// across cases and sequences stay per-case.
func TestConcurrentMultipleCasesIsolation(t *testing.T) {
	db := newTestDB(t)
	ids := []string{"c1", "c2", "c3"}
	for _, id := range ids {
		seedCase(t, db, id)
	}
	app := New(db)
	var wg sync.WaitGroup
	for i := 0; i < 30; i++ {
		for _, id := range ids {
			wg.Add(1)
			go func(id string, i int) {
				defer wg.Done()
				_, err := app.Append(context.Background(), AppendInput{
					CaseID: id, EventType: domain.EventNote, Actor: "a",
					Payload: note{N: i},
				})
				if err != nil {
					t.Errorf("append %s: %v", id, err)
				}
			}(id, i)
		}
	}
	wg.Wait()
	for i, id := range ids {
		rep, err := app.Verify(context.Background(), id)
		if err != nil || !rep.Intact || rep.HeadSeq != 30 {
			t.Fatalf("case %d (%s): intact=%v head=%d err=%v issues=%v",
				i, id, rep.Intact, rep.HeadSeq, err, rep.Issues)
		}
	}
}

func TestAppendUnknownCase(t *testing.T) {
	db := newTestDB(t)
	app := New(db)
	_, err := app.Append(context.Background(), AppendInput{
		CaseID: "ghost", EventType: domain.EventNote, Actor: "a", Payload: note{N: 1},
	})
	if err != ErrCaseNotFound {
		t.Fatalf("expected ErrCaseNotFound, got %v", err)
	}
}

func TestCanonicalStability(t *testing.T) {
	// Guard against accidental canonicalization drift.
	a, err := hashing.CanonicalJSON("cid", domain.EventNote, "actor",
		mustTime("2026-09-20T12:00:00.123456789Z"),
		struct {
			A string `json:"a"`
			B int    `json:"b"`
		}{A: "x", B: 7})
	if err != nil {
		t.Fatal(err)
	}
	b, err := hashing.CanonicalJSON("cid", domain.EventNote, "actor",
		mustTime("2026-09-20T12:00:00.123456789Z"),
		struct {
			B int    `json:"b"`
			A string `json:"a"`
		}{B: 7, A: "x"})
	if err != nil {
		t.Fatal(err)
	}
	if a != b {
		t.Fatalf("canonical JSON depends on Go field order:\n%s\n%s", a, b)
	}
	if got, want := a, fmt.Sprintf(`{"v":1,"case_id":"cid","event_type":"note","actor":"actor","ts":"2026-09-20T12:00:00.123456789Z","payload":{"a":"x","b":7}}`); got != want {
		t.Fatalf("canonical drift:\n got %s\nwant %s", got, want)
	}
}
