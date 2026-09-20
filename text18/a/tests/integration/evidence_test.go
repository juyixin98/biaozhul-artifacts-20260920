package integration

import (
	"context"
	"errors"
	"fmt"
	"sync/atomic"
	"testing"

	"sircc/internal/domain"
	"sircc/internal/service"
)

// Sequential fills reach exactly 50; the 51st is rejected.
func TestEvidence_HardCapSequential(t *testing.T) {
	f := newFixture(t)
	id := f.createP2Incident(t)

	for i := 0; i < domain.MaxEvidencePerIncident; i++ {
		if _, err := f.svc.AddEvidence(context.Background(), f.analyst1(), id, service.AddEvidenceInput{
			Content: fmt.Sprintf("evidence #%d", i+1),
		}); err != nil {
			t.Fatalf("add %d: %v", i+1, err)
		}
	}
	if _, err := f.svc.AddEvidence(context.Background(), f.analyst1(), id, service.AddEvidenceInput{
		Content: "one too many",
	}); !errors.Is(err, domain.ErrEvidenceCap) {
		t.Fatalf("want evidence cap, got %v", err)
	}

	items, err := f.svc.ListEvidence(context.Background(), f.analyst1(), id)
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if len(items) != domain.MaxEvidencePerIncident {
		t.Fatalf("want exactly %d items, got %d", domain.MaxEvidencePerIncident, len(items))
	}
	if items[0].Seq != 1 || items[49].Seq != 50 {
		t.Fatalf("sequencing wrong: first=%d last=%d", items[0].Seq, items[49].Seq)
	}
}

// Concurrent submits at the cap boundary serialize on the incident row
// lock; exactly one of the racing inserts can claim the last slot.
func TestEvidence_ConcurrentCannotBypassCap(t *testing.T) {
	f := newFixture(t)
	id := f.createP2Incident(t)

	for i := 0; i < domain.MaxEvidencePerIncident-1; i++ {
		if _, err := f.svc.AddEvidence(context.Background(), f.analyst1(), id, service.AddEvidenceInput{
			Content: fmt.Sprintf("pre %d", i),
		}); err != nil {
			t.Fatalf("prefill: %v", err)
		}
	}

	var ok, capped int32
	runConcurrently(12, func(i int) {
		_, err := f.svc.AddEvidence(context.Background(), f.analyst1(), id, service.AddEvidenceInput{
			Content: fmt.Sprintf("racer %d", i),
		})
		switch {
		case err == nil:
			atomic.AddInt32(&ok, 1)
		case errors.Is(err, domain.ErrEvidenceCap):
			atomic.AddInt32(&capped, 1)
		default:
			t.Errorf("unexpected err: %v", err)
		}
	})
	if ok != 1 {
		t.Fatalf("want exactly 1 racer to win, got %d (capped=%d)", ok, capped)
	}
	if capped != 11 {
		t.Fatalf("want 11 capped, got %d", capped)
	}
	items, _ := f.svc.ListEvidence(context.Background(), f.analyst1(), id)
	if len(items) != 50 {
		t.Fatalf("cap bypassed: %d items", len(items))
	}
}

// Submitted evidence is immutable; corrections are appended notes that leave
// the original body untouched.
func TestEvidence_AppendOnlyCorrections(t *testing.T) {
	f := newFixture(t)
	id := f.createP2Incident(t)

	res, err := f.svc.AddEvidence(context.Background(), f.analyst1(), id, service.AddEvidenceInput{
		Content: "initial observation",
	})
	if err != nil {
		t.Fatalf("add: %v", err)
	}
	evID := mustEvidenceID(t, res)

	// Another assigned analyst appends a correction note.
	f.assignAnalyst(t, id, f.analyst2().ID)
	if _, err := f.svc.AddEvidenceNote(context.Background(), f.analyst2(), id, evID,
		"correction: timestamp was off by 5 minutes"); err != nil {
		t.Fatalf("assigned analyst adds note: %v", err)
	}

	items, err := f.svc.ListEvidence(context.Background(), f.analyst1(), id)
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if len(items) != 1 {
		t.Fatalf("want 1 evidence item, got %d", len(items))
	}
	if items[0].Content != "initial observation" {
		t.Fatalf("original content mutated: %q", items[0].Content)
	}
	if len(items[0].Notes) != 1 || items[0].Notes[0].Note == "" {
		t.Fatalf("correction note missing: %+v", items[0].Notes)
	}
}

// A note cannot attach to evidence belonging to a different case.
func TestEvidence_NoteRejectsCrossCase(t *testing.T) {
	f := newFixture(t)
	a := f.createP2Incident(t)
	b := f.createP2Incident(t)
	res, err := f.svc.AddEvidence(context.Background(), f.analyst1(), a, service.AddEvidenceInput{
		Content: "case A evidence",
	})
	if err != nil {
		t.Fatalf("add: %v", err)
	}
	evID := mustEvidenceID(t, res)

	// analyst1 created both cases, so they have scope to both; a mismatched
	// path must still be a not-found rather than attaching across cases.
	_, err = f.svc.AddEvidenceNote(context.Background(), f.analyst1(), b, evID, "wrong case")
	if !errors.Is(err, domain.ErrNotFound) {
		t.Fatalf("want not found for cross-case note, got %v", err)
	}
}
