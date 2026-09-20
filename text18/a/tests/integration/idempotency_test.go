package integration

import (
	"context"
	"sync/atomic"
	"testing"

	"github.com/google/uuid"

	"sircc/internal/domain"
	"sircc/internal/service"
)

// Repeating a transition with the same request id returns the original
// result and advances the case only once.
func TestIdempotency_TransitionReplaysOriginal(t *testing.T) {
	f := newFixture(t)
	id := f.createP2Incident(t)
	reqID := uuid.New()

	do := func() (service.Result, error) {
		return f.svc.Transition(context.Background(), f.analyst1(), id, service.TransitionInput{
			Action:          "triage",
			ExpectedVersion: 1,
			RequestID:       reqID,
		})
	}
	first, err := do()
	if err != nil {
		t.Fatalf("first: %v", err)
	}
	second, err := do()
	if err != nil {
		t.Fatalf("duplicate: %v", err)
	}
	if second.Replay == nil {
		t.Fatalf("duplicate request should replay stored response")
	}
	b1 := first.Body.(*service.IncidentDetail)
	if string(second.Replay) == "" {
		t.Fatalf("empty replay body")
	}
	// The stored body must be the same incident version (2), not a second bump.
	d := f.get(t, id)
	if d.Version != 2 || b1.Version != 2 {
		t.Fatalf("version advanced twice: first=%d current=%d", b1.Version, d.Version)
	}
}

// Concurrent requests carrying one request id produce a single incident;
// every loser replays the same stored id.
func TestIdempotency_ConcurrentSameKeyCreatesOnce(t *testing.T) {
	f := newFixture(t)
	reqID := uuid.New()
	var created, replayed, other int32
	var ids = make(chan uuid.UUID, 16)

	runConcurrently(10, func(i int) {
		res, err := f.svc.CreateIncident(context.Background(), f.analyst1(), service.CreateIncidentInput{
			Title:     "deduped incident",
			Severity:  "P3",
			RequestID: reqID,
		})
		switch {
		case err != nil:
			atomic.AddInt32(&other, 1)
			t.Errorf("unexpected: %v", err)
		case res.Replay != nil:
			atomic.AddInt32(&replayed, 1)
		default:
			atomic.AddInt32(&created, 1)
			ids <- uuid.MustParse(res.Body.(*service.IncidentDetail).ID)
		}
	})
	if created != 1 || replayed != 9 {
		t.Fatalf("created=%d replayed=%d other=%d", created, replayed, other)
	}
	incidents, _, err := f.svc.ListIncidents(context.Background(), f.admin(), "", 0, 0)
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if len(incidents) != 1 {
		t.Fatalf("want a single incident, got %d", len(incidents))
	}
}

// A request id reused for a different operation (different method/path) is
// rejected rather than silently replaying an unrelated result.
func TestIdempotency_KeyReuseDifferentRequestRejected(t *testing.T) {
	f := newFixture(t)
	id := f.createP2Incident(t)
	reqID := uuid.New()

	// Original use: triage.
	if _, err := f.svc.Transition(context.Background(), f.analyst1(), id, service.TransitionInput{
		Action: "triage", ExpectedVersion: 1, RequestID: reqID,
	}); err != nil {
		t.Fatalf("original: %v", err)
	}
	// Reuse the same key on evidence creation (different path).
	_, err := f.svc.AddEvidence(context.Background(), f.analyst1(), id, service.AddEvidenceInput{
		Content:   "reused key",
		RequestID: reqID,
	})
	if err != domain.ErrKeyReuse {
		t.Fatalf("want key reuse error, got %v", err)
	}
}

// Evidence dedupe: repeated request id returns the same evidence row and
// does not consume an extra slot.
func TestIdempotency_EvidenceDedupe(t *testing.T) {
	f := newFixture(t)
	id := f.createP2Incident(t)
	reqID := uuid.New()
	in := service.AddEvidenceInput{Content: "deduped evidence", RequestID: reqID}

	r1, err := f.svc.AddEvidence(context.Background(), f.analyst1(), id, in)
	if err != nil {
		t.Fatalf("first: %v", err)
	}
	r2, err := f.svc.AddEvidence(context.Background(), f.analyst1(), id, in)
	if err != nil {
		t.Fatalf("duplicate: %v", err)
	}
	if r2.Replay == nil {
		t.Fatalf("expected replay")
	}
	v := r1.Body.(service.EvidenceView)
	if v.ID == "" {
		t.Fatalf("missing id")
	}
	items, _ := f.svc.ListEvidence(context.Background(), f.analyst1(), id)
	if len(items) != 1 || items[0].Seq != 1 {
		t.Fatalf("duplicate consumed a slot: %+v", items)
	}
}
