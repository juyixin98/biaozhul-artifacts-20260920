package integration

import (
	"context"
	"errors"
	"sync/atomic"
	"testing"

	"sircc/internal/domain"
	"sircc/internal/service"
)

// Two analysts triaging concurrently on the same expected version: exactly
// one must win; the loser sees a stale-version rejection. No skipped phase
// and no duplicate phase row are possible.
func TestTransition_ConcurrentSameVersion_ExactlyOneSucceeds(t *testing.T) {
	f := newFixture(t)
	id := f.createP2Incident(t)

	var ok, stale, other int32
	runConcurrently(8, func(i int) {
		_, err := f.svc.Transition(context.Background(), f.analyst1(), id, service.TransitionInput{
			Action:          "triage",
			ExpectedVersion: 1,
		})
		switch {
		case err == nil:
			atomic.AddInt32(&ok, 1)
		case errors.Is(err, domain.ErrStaleVersion):
			atomic.AddInt32(&stale, 1)
		default:
			atomic.AddInt32(&other, 1)
			t.Errorf("unexpected error: %v", err)
		}
	})

	if ok != 1 {
		t.Fatalf("want exactly 1 successful transition, got %d (stale=%d other=%d)", ok, stale, other)
	}
	if stale != 7 {
		t.Fatalf("want 7 stale rejections, got %d", stale)
	}
	d := f.get(t, id)
	if d.Status != domain.StatusTriaged || d.Version != 2 {
		t.Fatalf("unexpected post state status=%s version=%d", d.Status, d.Version)
	}
	// Detection + triage only — no phase may have been inserted twice.
	phaseSeen := map[string]int{}
	for _, p := range d.Phases {
		phaseSeen[p.Phase]++
	}
	if phaseSeen[domain.PhaseDetection] != 1 || phaseSeen[domain.PhaseTriage] != 1 {
		t.Fatalf("unexpected phase rows: %#v", phaseSeen)
	}
}

// A stale expected version (caller knows version 1, case already at 3) is
// rejected even by an authorized responder.
func TestTransition_StaleVersionRejected(t *testing.T) {
	f := newFixture(t)
	id := f.createP2Incident(t)
	f.assignResponder(t, id, f.responder1().ID)
	f.mustTransition(t, f.analyst1(), id, "triage", 1)
	f.mustTransition(t, f.responder1(), id, "contain", 2)

	_, err := f.svc.Transition(context.Background(), f.responder1(), id, service.TransitionInput{
		Action:          "eradicate",
		ExpectedVersion: 1, // stale
	})
	if !errors.Is(err, domain.ErrStaleVersion) {
		t.Fatalf("want stale version, got %v", err)
	}
}

// Phases cannot be skipped: containment cannot be attempted straight from
// detected.
func TestTransition_CannotSkipPhase(t *testing.T) {
	f := newFixture(t)
	id := f.createP2Incident(t)
	f.assignResponder(t, id, f.responder1().ID)

	_, err := f.svc.Transition(context.Background(), f.responder1(), id, service.TransitionInput{
		Action:          "contain",
		ExpectedVersion: 1, // still detected: triage missing
	})
	if !errors.Is(err, domain.ErrInvalidTransition) {
		t.Fatalf("want invalid transition, got %v", err)
	}
	d := f.get(t, id)
	if d.Status != domain.StatusDetected {
		t.Fatalf("status changed despite rejected transition: %s", d.Status)
	}
}

// P1 cannot be triaged until an admin assigns a responder.
func TestTransition_P1TriageGate(t *testing.T) {
	f := newFixture(t)
	id := f.createIncident(t, f.analyst1(), "P1", "P1 outage")

	_, err := f.svc.Transition(context.Background(), f.analyst1(), id, service.TransitionInput{
		Action:          "triage",
		ExpectedVersion: 1,
	})
	if !errors.Is(err, domain.ErrTriageGate) {
		t.Fatalf("want triage gate, got %v", err)
	}

	// Non-admin cannot satisfy the gate by assigning responders.
	if _, err := f.svc.AssignMember(context.Background(), f.analyst1(), id,
		f.responder1().ID, "responder"); !errors.Is(err, domain.ErrForbidden) {
		t.Fatalf("non-admin assignment should be forbidden, got %v", err)
	}

	f.assignResponder(t, id, f.responder1().ID)
	f.mustTransition(t, f.analyst1(), id, "triage", 1)
}

// Closure requires root cause, lessons learned and an effective action item
// with owner + deadline.
func TestTransition_ClosureGate(t *testing.T) {
	f := newFixture(t)
	id := f.createP2Incident(t)
	f.assignResponder(t, id, f.responder1().ID)
	f.mustTransition(t, f.analyst1(), id, "triage", 1)
	f.mustTransition(t, f.responder1(), id, "contain", 2)
	f.mustTransition(t, f.responder1(), id, "eradicate", 3)
	f.mustTransition(t, f.responder1(), id, "recover", 4)
	f.mustTransition(t, f.analyst1(), id, "review", 5)

	// Close with nothing: rejected.
	_, err := f.svc.Transition(context.Background(), f.admin(), id, service.TransitionInput{
		Action:          "close",
		ExpectedVersion: 6,
	})
	if !errors.Is(err, domain.ErrClosureGate) {
		t.Fatalf("want closure gate (no artifacts), got %v", err)
	}

	// Close with root cause + lessons but no action item: still rejected.
	_, err = f.svc.Transition(context.Background(), f.admin(), id, service.TransitionInput{
		Action:          "close",
		ExpectedVersion: 6,
		RootCause:       "unpatched CVE",
		LessonsLearned:  "patch faster",
	})
	if !errors.Is(err, domain.ErrClosureGate) {
		t.Fatalf("want closure gate (no action item), got %v", err)
	}

	// Add an owned, deadline-bearing action item, then close succeeds.
	if _, err := f.svc.CreateActionItem(context.Background(), f.analyst1(), id, service.CreateActionItemInput{
		Description: "harden baseline",
		OwnerUserID: f.responder1().ID,
		DueAt:       f.clk.Now().AddDate(0, 1, 0),
	}); err != nil {
		t.Fatalf("create action item: %v", err)
	}
	closed := f.mustTransitionWithArtifacts(t, f.admin(), id, 6)
	if closed.Status != domain.StatusClosed || closed.ClosedAt == nil {
		t.Fatalf("case not closed properly: status=%s closedAt=%v", closed.Status, closed.ClosedAt)
	}
	if closed.RootCause == nil || closed.LessonsLearned == nil {
		t.Fatalf("root cause / lessons not persisted: %+v", closed.IncidentView)
	}
}

// Status, phase row and audit event are committed together: after a
// successful transition there is exactly one audit event per action and one
// phase row per status.
func TestTransition_AtomicStatusPhaseAudit(t *testing.T) {
	f := newFixture(t)
	id := f.createP2Incident(t)
	f.assignResponder(t, id, f.responder1().ID)
	f.mustTransition(t, f.analyst1(), id, "triage", 1)

	d := f.get(t, id)
	// created + responder assigned + triage transition
	if len(d.Audit) != 3 {
		t.Fatalf("want 3 audit events after create+assign+triage, got %d", len(d.Audit))
	}
	found := false
	for _, a := range d.Audit {
		if a.Action == "incident.transition.triage" {
			found = true
			if a.FromStatus == nil || *a.FromStatus != domain.StatusDetected ||
				a.ToStatus == nil || *a.ToStatus != domain.StatusTriaged {
				t.Fatalf("audit transition detail wrong: %+v", a)
			}
		}
	}
	if !found {
		t.Fatalf("triage audit event missing: %+v", d.Audit)
	}
}
