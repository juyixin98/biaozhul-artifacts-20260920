package integration

import (
	"context"
	"testing"
	"time"

	"sircc/internal/domain"
	"sircc/internal/service"
)

// Role separation: responders cannot triage; analysts cannot drive
// containment; neither can assign people (admin only).
func TestRBAC_RoleSeparation(t *testing.T) {
	f := newFixture(t)
	id := f.createP2Incident(t)
	f.assignResponder(t, id, f.responder1().ID)

	// Responder tries triage.
	if _, err := f.svc.Transition(context.Background(), f.responder1(), id, service.TransitionInput{
		Action: "triage", ExpectedVersion: 1,
	}); err != domain.ErrForbidden {
		t.Fatalf("responder triage: want forbidden, got %v", err)
	}
	// Analyst tries containment (case still detected, but role checked first).
	if _, err := f.svc.Transition(context.Background(), f.analyst1(), id, service.TransitionInput{
		Action: "contain", ExpectedVersion: 1,
	}); err != domain.ErrForbidden {
		t.Fatalf("analyst contain: want forbidden, got %v", err)
	}
	// Analyst assigns a responder: forbidden.
	if _, err := f.svc.AssignMember(context.Background(), f.analyst1(), id,
		f.responder2().ID, "responder"); err != domain.ErrForbidden {
		t.Fatalf("analyst assign: want forbidden, got %v", err)
	}
}

// Case scope: a user who is not a member (and not an admin) cannot read,
// transition, add evidence or create action items on a case.
func TestRBAC_CaseScopeEnforced(t *testing.T) {
	f := newFixture(t)
	id := f.createIncident(t, f.analyst1(), "P2", "scoped case")
	f.assignResponder(t, id, f.responder1().ID)
	// analyst1 must triage so responders can act; do it.
	f.mustTransition(t, f.analyst1(), id, "triage", 1)

	// analyst2 is not a member: reads and writes are forbidden.
	if _, err := f.svc.GetIncident(context.Background(), f.analyst2(), id, true); err != domain.ErrForbidden {
		t.Fatalf("outsider read: want forbidden, got %v", err)
	}
	if _, err := f.svc.AddEvidence(context.Background(), f.analyst2(), id,
		service.AddEvidenceInput{Content: "x"}); err != domain.ErrForbidden {
		t.Fatalf("outsider evidence: want forbidden, got %v", err)
	}
	if _, err := f.svc.Transition(context.Background(), f.responder2(), id, service.TransitionInput{
		Action: "contain", ExpectedVersion: 2,
	}); err != domain.ErrForbidden {
		t.Fatalf("unassigned responder contain: want forbidden, got %v", err)
	}
	if _, err := f.svc.CreateActionItem(context.Background(), f.analyst2(), id, service.CreateActionItemInput{
		Description: "x", OwnerUserID: f.responder1().ID, DueAt: f.clk.Now().Add(time.Hour),
	}); err != domain.ErrForbidden {
		t.Fatalf("outsider action item: want forbidden, got %v", err)
	}

	// Admin retains access and can act across cases.
	d, err := f.svc.GetIncident(context.Background(), f.admin(), id, false)
	if err != nil || d == nil {
		t.Fatalf("admin cross-case read failed: %v", err)
	}
}

// Listing is scoped: a non-admin only sees cases they belong to.
func TestRBAC_ListScopedToMembership(t *testing.T) {
	f := newFixture(t)
	f.createIncident(t, f.analyst1(), "P2", "analyst1 case")
	f.createIncident(t, f.analyst2(), "P2", "analyst2 case")

	a1, _, err := f.svc.ListIncidents(context.Background(), f.analyst1(), "", 0, 0)
	if err != nil {
		t.Fatalf("list a1: %v", err)
	}
	if len(a1) != 1 || a1[0].Title != "analyst1 case" {
		t.Fatalf("analyst1 scope wrong: %+v", a1)
	}
	all, _, err := f.svc.ListIncidents(context.Background(), f.admin(), "", 0, 0)
	if err != nil {
		t.Fatalf("list admin: %v", err)
	}
	if len(all) != 2 {
		t.Fatalf("admin should see both cases, got %d", len(all))
	}
}

// An assigned responder on case A cannot act on case B even though they are
// a legitimate responder — membership is per case.
func TestRBAC_MembershipIsPerCase(t *testing.T) {
	f := newFixture(t)
	a := f.createP2Incident(t)
	b := f.createP2Incident(t)
	f.assignResponder(t, a, f.responder1().ID)
	f.mustTransition(t, f.analyst1(), a, "triage", 1)
	// Case b is independently created; responder1 is not assigned there.
	if _, err := f.svc.Transition(context.Background(), f.responder1(), b, service.TransitionInput{
		Action: "triage", ExpectedVersion: 1,
	}); err != domain.ErrForbidden {
		t.Fatalf("cross-case responder: want forbidden, got %v", err)
	}
}
