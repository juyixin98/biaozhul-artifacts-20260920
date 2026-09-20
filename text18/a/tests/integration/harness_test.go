package integration

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"sircc/internal/clock"
	"sircc/internal/service"
	"sircc/internal/store"
	"sircc/internal/testsupport"
)

type fixture struct {
	t    *testing.T
	pool *pgxpool.Pool
	svc  *service.Service
	clk  *clock.Mock
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	pool := testsupport.NewPool(t)
	testsupport.ResetDB(t, pool)
	clk := &clock.Mock{T: time.Date(2026, 9, 1, 8, 0, 0, 0, time.UTC)}
	return &fixture{t: t, pool: pool, svc: service.New(pool, clk), clk: clk}
}

func (f *fixture) user(t *testing.T, id uuid.UUID) *store.User {
	t.Helper()
	u, err := store.New(f.pool).UserByID(context.Background(), id)
	if err != nil {
		t.Fatalf("load user %s: %v", id, err)
	}
	return &u
}

func (f *fixture) admin() *store.User    { return f.mustUser(testsupport.AdminID) }
func (f *fixture) analyst1() *store.User { return f.mustUser(testsupport.Analyst1ID) }
func (f *fixture) analyst2() *store.User { return f.mustUser(testsupport.Analyst2ID) }
func (f *fixture) responder1() *store.User {
	return f.mustUser(testsupport.Responder1ID)
}
func (f *fixture) responder2() *store.User {
	return f.mustUser(testsupport.Responder2ID)
}

func (f *fixture) mustUser(id uuid.UUID) *store.User {
	u, err := store.New(f.pool).UserByID(context.Background(), id)
	if err != nil {
		f.t.Fatalf("load user: %v", err)
	}
	return &u
}

// createP2Incident builds an incident owned by analyst1.
func (f *fixture) createP2Incident(t *testing.T) uuid.UUID {
	t.Helper()
	return f.createIncident(t, f.analyst1(), "P2", "P2 case")
}

func (f *fixture) createIncident(t *testing.T, creator *store.User, severity, title string) uuid.UUID {
	t.Helper()
	res, err := f.svc.CreateIncident(context.Background(), creator, service.CreateIncidentInput{
		Title:    title,
		Severity: severity,
	})
	if err != nil {
		t.Fatalf("create incident: %v", err)
	}
	return uuid.MustParse(res.Body.(*service.IncidentDetail).ID)
}

func (f *fixture) assignResponder(t *testing.T, incidentID, responderID uuid.UUID) {
	t.Helper()
	if _, err := f.svc.AssignMember(context.Background(), f.admin(), incidentID, responderID, "responder"); err != nil {
		t.Fatalf("assign responder: %v", err)
	}
}

func (f *fixture) assignAnalyst(t *testing.T, incidentID, analystID uuid.UUID) {
	t.Helper()
	if _, err := f.svc.AssignMember(context.Background(), f.admin(), incidentID, analystID, "analyst"); err != nil {
		t.Fatalf("assign analyst: %v", err)
	}
}

func (f *fixture) get(t *testing.T, incidentID uuid.UUID) *service.IncidentDetail {
	t.Helper()
	d, err := f.svc.GetIncident(context.Background(), f.admin(), incidentID, true)
	if err != nil {
		t.Fatalf("get incident: %v", err)
	}
	return d
}

// mustTransition drives a single transition expected to succeed.
func (f *fixture) mustTransition(t *testing.T, caller *store.User, incidentID uuid.UUID,
	action string, version int64,
) *service.IncidentDetail {
	t.Helper()
	res, err := f.svc.Transition(context.Background(), caller, incidentID, service.TransitionInput{
		Action:          action,
		ExpectedVersion: version,
	})
	if err != nil {
		t.Fatalf("transition %s v%d: %v", action, version, err)
	}
	return res.Body.(*service.IncidentDetail)
}

// mustTransitionWithArtifacts closes a case carrying the review artifacts.
func (f *fixture) mustTransitionWithArtifacts(t *testing.T, caller *store.User,
	incidentID uuid.UUID, version int64,
) *service.IncidentDetail {
	t.Helper()
	res, err := f.svc.Transition(context.Background(), caller, incidentID, service.TransitionInput{
		Action:          "close",
		ExpectedVersion: version,
		RootCause:       "unpatched CVE-2026-X",
		LessonsLearned:  "accelerate patching and validate detection coverage",
	})
	if err != nil {
		t.Fatalf("close v%d: %v", version, err)
	}
	return res.Body.(*service.IncidentDetail)
}

func isErr(err, target error) bool { return errors.Is(err, target) }

// mustEvidenceID extracts the evidence id from an AddEvidence result.
func mustEvidenceID(t *testing.T, res service.Result) uuid.UUID {
	t.Helper()
	v, ok := res.Body.(service.EvidenceView)
	if !ok {
		t.Fatalf("unexpected evidence result body: %T", res.Body)
	}
	id, err := uuid.Parse(v.ID)
	if err != nil {
		t.Fatalf("parse evidence id: %v", err)
	}
	return id
}

// runConcurrently invokes fn n times in separate goroutines and waits.
func runConcurrently(n int, fn func(i int)) {
	var wg sync.WaitGroup
	wg.Add(n)
	for i := 0; i < n; i++ {
		go func(i int) { defer wg.Done(); fn(i) }(i)
	}
	wg.Wait()
}
