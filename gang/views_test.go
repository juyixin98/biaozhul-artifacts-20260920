package gang

import (
	"context"
	"errors"
	"testing"
	"time"
)

// Exercise the read-only projections and the option constructors.
func TestViewsAndOptions(t *testing.T) {
	s := NewScheduler(
		WithDefaultTTL(7*time.Second),
		WithSweepInterval(50*time.Millisecond),
	)
	if got := s.DefaultTTL(); got != 7*time.Second {
		t.Fatalf("default ttl = %v, want 7s", got)
	}
	if s.sweep != 50*time.Millisecond {
		t.Fatalf("sweep = %v", s.sweep)
	}

	ctx, cancel := context.WithCancel(context.Background())
	s.Start(ctx) // starts the reaper goroutine (covered paths)
	mustAddNode(t, s, "n1", "z1", 2, map[string]string{"disk": "ssd", "zone": "custom"})
	g, p, err := s.SubmitGang(gangSpec("g1", 1, func(sp *GangSpec) {
		sp.Tasks[0].NodeSelector = map[string]string{"disk": "ssd"}
	}))
	if err != nil {
		t.Fatal(err)
	}

	st := s.GetState()
	if len(st.Nodes) != 1 || len(st.Gangs) != 1 || len(st.Plans) != 1 {
		t.Fatalf("state = %+v", st)
	}
	if st.Nodes[0].HeldSlots != 1 || st.Plans[0].TTLRemaining <= 0 {
		t.Fatalf("node/plan views wrong: %+v %+v", st.Nodes[0], st.Plans[0])
	}
	gv, pv, err := s.GetGang("g1")
	if err != nil || pv == nil || gv.ActivePlanID != p.ID {
		t.Fatalf("GetGang: %v %v %v", gv, pv, err)
	}
	if _, err := s.GetNode("n1"); err != nil {
		t.Fatal(err)
	}
	if _, err := s.GetPlan("g1", p.ID); err != nil {
		t.Fatal(err)
	}
	for _, bad := range []func() error{
		func() error { _, _, e := s.GetGang("nope"); return e },
		func() error { _, e := s.GetNode("nope"); return e },
		func() error { _, e := s.GetPlan("g1", "nope"); return e },
	} {
		if !errors.Is(bad(), ErrNotFound) {
			t.Fatal("want not found")
		}
	}

	// affinityValue prefers an explicit "zone" label over the zone field.
	if v := affinityValue(s.nodes["n1"], "zone"); v != "custom" {
		t.Fatalf("affinity zone = %q, want label override custom", v)
	}
	if v := affinityValue(s.nodes["n1"], "disk"); v != "ssd" {
		t.Fatalf("affinity disk = %q", v)
	}
	if v := affinityValue(s.nodes["n1"], "missing"); v != "" {
		t.Fatalf("missing key = %q, want empty", v)
	}

	// Background reaper lifecycle: stop cleanly and can restart not needed.
	cancel()
	s.Stop()
	_ = g
}

// Error/status branches that the main table does not already hit.
func TestErrorBranches(t *testing.T) {
	s, _ := newTestScheduler(t)
	mustAddNode(t, s, "n1", "z", 1, nil)
	mustAddNode(t, s, "n2", "z", 1, nil)

	// Duplicate gang.
	g, p, _ := s.SubmitGang(gangSpec("g1", 1, nil))
	if _, _, err := s.SubmitGang(gangSpec("g1", 1, nil)); !errors.Is(err, ErrConflict) {
		t.Fatalf("dup gang: %v", err)
	}

	// Plan addressed under the wrong gang id -> conflict.
	mustErr := func(err error, want error, msg string) {
		t.Helper()
		if !errors.Is(err, want) {
			t.Fatalf("%s: err=%v want %v", msg, err, want)
		}
	}
	_, _, err := s.CommitPlan("ghost", p.ID, p.Version)
	mustErr(err, ErrNotFound, "commit unknown gang")
	_, err = s.ReleasePlan("ghost", p.ID)
	mustErr(err, ErrNotFound, "release unknown gang")
	_, err = s.CompleteGang("ghost")
	mustErr(err, ErrNotFound, "complete unknown gang")

	// Retry on a non-waiting gang -> conflict.
	_, _, err = s.RetryReserve("g1")
	mustErr(err, ErrConflict, "retry reserved gang")
	// Unknown gang retry.
	_, _, err = s.RetryReserve("ghost")
	mustErr(err, ErrNotFound, "retry unknown gang")

	// SetNodeOnline: unknown node, and idempotent same-state call.
	mustErr(s.SetNodeOnline("ghost", false), ErrNotFound, "offline unknown node")
	if err := s.SetNodeOnline("n1", true); err != nil {
		t.Fatalf("idempotent online: %v", err)
	}

	// Commit with a made-up plan id, release a non-reserved plan, complete a
	// non-running gang.
	_, _, err = s.CommitPlan("g1", "p999", 1)
	mustErr(err, ErrNotFound, "commit unknown plan")
	if _, _, err := s.CommitPlan("g1", p.ID, p.Version); err != nil {
		t.Fatal(err)
	}
	_, err = s.ReleasePlan("g1", p.ID)
	mustErr(err, ErrConflict, "release committed plan")
	_, err = s.CompleteGang("g1")
	if err != nil {
		t.Fatal(err)
	}
	// g1 is now succeeded: complete again conflicts.
	_, err = s.CompleteGang("g1")
	mustErr(err, ErrConflict, "double complete")
	_ = g
}

// A plan created for gang B must not be committable through gang A's path.
func TestPlanOwnershipMismatch(t *testing.T) {
	s, _ := newTestScheduler(t)
	mustAddNode(t, s, "n1", "z", 1, nil)
	mustAddNode(t, s, "n2", "z", 1, nil)
	_, p1, _ := s.SubmitGang(gangSpec("gA", 1, nil))
	// Finish gA so n1 frees, then gB reserves a plan.
	if _, _, err := s.CommitPlan("gA", p1.ID, p1.Version); err != nil {
		t.Fatal(err)
	}
	if _, err := s.CompleteGang("gA"); err != nil {
		t.Fatal(err)
	}
	_, p2, _ := s.SubmitGang(gangSpec("gB", 1, nil))
	if _, _, err := s.CommitPlan("gA", p2.ID, p2.Version); !errors.Is(err, ErrConflict) {
		t.Fatalf("commit gB's plan via gA: err=%v, want conflict", err)
	}
}
