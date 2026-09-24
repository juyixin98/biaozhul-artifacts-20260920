package gang

import (
	"errors"
	"testing"
)

// testClock is a manually-advanced millisecond clock for deterministic TTL
// tests.
type testClock struct{ t int64 }

func (c *testClock) now() int64 { return c.t }

func newTestScheduler(t *testing.T) (*Scheduler, *testClock) {
	t.Helper()
	c := &testClock{t: 1_000_000}
	s := NewScheduler(WithClock(c.now))
	t.Cleanup(s.Stop)
	return s, c
}

func mustAddNode(t *testing.T, s *Scheduler, id, zone string, cap int, labels map[string]string) {
	t.Helper()
	if err := s.AddNode(id, zone, labels, cap); err != nil {
		t.Fatalf("AddNode(%s): %v", id, err)
	}
}

func gangSpec(id string, n int, extra func(*GangSpec)) GangSpec {
	spec := GangSpec{ID: id, MinNodes: n, TTLMillis: 60_000}
	for i := 0; i < n; i++ {
		spec.Tasks = append(spec.Tasks, TaskSpec{Name: "t" + string(rune('a'+i))})
	}
	if extra != nil {
		extra(&spec)
	}
	return spec
}

// assertNoLeaks is the global resource invariant: no node exceeds capacity,
// every held slot points at a reserved plan, every running slot at a running
// gang, and every reserved plan's slot count matches the nodes it picked.
func assertNoLeaks(t *testing.T, s *Scheduler) {
	t.Helper()
	for _, n := range s.nodes {
		if len(n.reservedBy)+len(n.runningBy) > n.Capacity {
			t.Fatalf("node %s over capacity: %d held + %d running > %d",
				n.ID, len(n.reservedBy), len(n.runningBy), n.Capacity)
		}
		for _, pid := range n.reservedBy {
			p, ok := s.plans[pid]
			if !ok || p.Status != planReserved {
				t.Fatalf("node %s holds slot for non-reserved plan %s", n.ID, pid)
			}
		}
		for _, gid := range n.runningBy {
			g, ok := s.gangs[gid]
			if !ok || g.Status != statusRunning {
				t.Fatalf("node %s runs slot for non-running gang %s", n.ID, gid)
			}
		}
	}
	for _, p := range s.plans {
		if p.Status != planReserved {
			continue
		}
		held := 0
		for _, n := range s.nodes {
			for _, pid := range n.reservedBy {
				if pid == p.ID {
					held++
				}
			}
		}
		if held != len(p.Nodes) {
			t.Fatalf("plan %s holds %d slots but picked %d nodes", p.ID, held, len(p.Nodes))
		}
	}
}

func equalSet(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	m := map[string]int{}
	for _, x := range a {
		m[x]++
	}
	for _, x := range b {
		m[x]--
	}
	for _, v := range m {
		if v != 0 {
			return false
		}
	}
	return true
}

// 1. Basic all-or-nothing reserve -> commit happy path.
func TestReserveAndCommitAtomic(t *testing.T) {
	s, _ := newTestScheduler(t)
	mustAddNode(t, s, "n1", "z1", 1, nil)
	mustAddNode(t, s, "n2", "z1", 1, nil)

	g, p, err := s.SubmitGang(gangSpec("g1", 2, nil))
	if err != nil {
		t.Fatal(err)
	}
	if g.Status != statusReserved || p == nil {
		t.Fatalf("expected reserved with plan, got %s / %v", g.Status, p)
	}
	if len(p.Nodes) != 2 {
		t.Fatalf("expected 2 nodes, got %v", p.Nodes)
	}

	g, p, err = s.CommitPlan("g1", p.ID, p.Version)
	if err != nil {
		t.Fatalf("commit: %v", err)
	}
	if g.Status != statusRunning || p.Status != planCommitted {
		t.Fatalf("expected running/committed, got %s/%s", g.Status, p.Status)
	}
	for _, nid := range p.Nodes {
		if got := len(s.nodes[nid].runningBy); got != 1 {
			t.Fatalf("node %s running slots = %d, want 1", nid, got)
		}
	}
	assertNoLeaks(t, s)
}

// 2. A gang that cannot be fully placed waits and holds nothing.
func TestInsufficientNodesWaits(t *testing.T) {
	s, _ := newTestScheduler(t)
	mustAddNode(t, s, "n1", "z1", 1, nil)

	g, p, err := s.SubmitGang(gangSpec("g1", 2, nil))
	if err != nil {
		t.Fatal(err)
	}
	if g.Status != statusWaiting || p != nil {
		t.Fatalf("expected waiting/no plan, got %s/%v", g.Status, p)
	}
	if len(s.queued) != 1 {
		t.Fatalf("waiting queue = %v", s.queued)
	}
	assertNoLeaks(t, s)

	// Adding the second node lets the queued gang reserve immediately.
	mustAddNode(t, s, "n2", "z1", 1, nil)
	if g.Status != statusReserved {
		t.Fatalf("after adding node, status = %s", g.Status)
	}
	assertNoLeaks(t, s)
}

// 3. Headline competition case: two gangs whose node sets overlap. The
// second waits holding ZERO nodes, then atomically reserves only after the
// first finishes and frees everything.
func TestCompetingGangsOverlapping(t *testing.T) {
	s, _ := newTestScheduler(t)
	mustAddNode(t, s, "n1", "z", 1, map[string]string{"role": "a"})
	mustAddNode(t, s, "n2", "z", 1, map[string]string{"role": "shared"})
	mustAddNode(t, s, "n3", "z", 1, map[string]string{"role": "b"})

	// gA: one task pinned to role=a, one to role=shared (n1+n2).
	gA := gangSpec("gA", 2, func(sp *GangSpec) {
		sp.Tasks[0].NodeSelector = map[string]string{"role": "a"}
		sp.Tasks[1].NodeSelector = map[string]string{"role": "shared"}
	})
	// gB: one pinned to role=shared, one to role=b (n2+n3).
	gB := gangSpec("gB", 2, func(sp *GangSpec) {
		sp.Tasks[0].NodeSelector = map[string]string{"role": "shared"}
		sp.Tasks[1].NodeSelector = map[string]string{"role": "b"}
	})

	g1, p1, err := s.SubmitGang(gA)
	if err != nil {
		t.Fatal(err)
	}
	if g1.Status != statusReserved {
		t.Fatalf("gA = %s, want reserved", g1.Status)
	}

	g2, p2, err := s.SubmitGang(gB)
	if err != nil {
		t.Fatal(err)
	}
	if g2.Status != statusWaiting || p2 != nil {
		t.Fatalf("gB = %s/%v, want waiting with no plan", g2.Status, p2)
	}
	// gB holds nothing — not even its free node n3.
	if slots := len(s.nodes["n3"].reservedBy) + len(s.nodes["n3"].runningBy); slots != 0 {
		t.Fatalf("n3 should be untouched, got %d slots", slots)
	}
	if !contains(s.queued, "gB") {
		t.Fatalf("gB missing from queue: %v", s.queued)
	}
	assertNoLeaks(t, s)

	// Commit gA, finish it; gB must then atomically take n2+n3.
	if _, _, err := s.CommitPlan("gA", p1.ID, p1.Version); err != nil {
		t.Fatal(err)
	}
	if _, err := s.CompleteGang("gA"); err != nil {
		t.Fatal(err)
	}
	if g2.Status != statusReserved || g2.ActivePlanID == "" {
		t.Fatalf("gB after gA completion = %s, want reserved", g2.Status)
	}
	pb := s.plans[g2.ActivePlanID]
	if !equalSet(pb.Nodes, []string{"n2", "n3"}) {
		t.Fatalf("gB reserved nodes = %v, want n2,n3", pb.Nodes)
	}
	if _, _, err := s.CommitPlan("gB", pb.ID, pb.Version); err != nil {
		t.Fatal(err)
	}
	if g2.Status != statusRunning {
		t.Fatalf("gB = %s, want running", g2.Status)
	}
	// No double booking.
	if got := s.nodes["n2"].runningBy; len(got) != 1 || got[0] != "gB" {
		t.Fatalf("n2 running = %v, want [gB]", got)
	}
	if got := s.nodes["n3"].runningBy; len(got) != 1 || got[0] != "gB" {
		t.Fatalf("n3 running = %v, want [gB]", got)
	}
	assertNoLeaks(t, s)
}

// 4. Acceptance case: a node goes offline between reserve and commit. The
// plan is invalidated in full; commit starts nothing and every held slot is
// freed — no partial start, no leak.
func TestNodeOfflineBetweenReserveAndCommit(t *testing.T) {
	s, _ := newTestScheduler(t)
	mustAddNode(t, s, "n1", "z", 1, nil)
	mustAddNode(t, s, "n2", "z", 1, nil)

	g, p, err := s.SubmitGang(gangSpec("g1", 2, nil))
	if err != nil {
		t.Fatal(err)
	}

	// Take n2 offline while g1 holds it (no running tasks, so allowed).
	if err := s.SetNodeOnline("n2", false); err != nil {
		t.Fatalf("offline n2: %v", err)
	}

	_, _, err = s.CommitPlan("g1", p.ID, p.Version)
	if !errors.Is(err, ErrExpired) {
		t.Fatalf("commit err = %v, want ErrExpired (410)", err)
	}
	for _, id := range []string{"n1", "n2"} {
		if len(s.nodes[id].reservedBy) != 0 {
			t.Fatalf("%s still held after abort: %v", id, s.nodes[id].reservedBy)
		}
		if len(s.nodes[id].runningBy) != 0 {
			t.Fatalf("%s started running tasks despite abort: %v", id, s.nodes[id].runningBy)
		}
	}
	if g.Status != statusWaiting {
		t.Fatalf("gang = %s, want waiting", g.Status)
	}

	// Bringing n2 back lets the gang reserve and commit cleanly.
	if err := s.SetNodeOnline("n2", true); err != nil {
		t.Fatal(err)
	}
	if g.Status != statusReserved {
		t.Fatalf("gang after n2 online = %s, want reserved", g.Status)
	}
	p2 := s.plans[g.ActivePlanID]
	if _, _, err := s.CommitPlan("g1", p2.ID, p2.Version); err != nil {
		t.Fatalf("second commit: %v", err)
	}
	if g.Status != statusRunning {
		t.Fatalf("gang = %s, want running", g.Status)
	}
	assertNoLeaks(t, s)
}

// 5. Optimistic concurrency: a wrong version rejects the commit; the plan is
// fully aborted and no node starts anything. Re-committing the aborted plan
// fails Gone.
func TestCommitVersionMismatch(t *testing.T) {
	s, _ := newTestScheduler(t)
	mustAddNode(t, s, "n1", "z", 1, nil)
	mustAddNode(t, s, "n2", "z", 1, nil)

	g, p, err := s.SubmitGang(gangSpec("g1", 2, nil))
	if err != nil {
		t.Fatal(err)
	}
	_, _, err = s.CommitPlan("g1", p.ID, p.Version-1)
	if !errors.Is(err, ErrConflict) {
		t.Fatalf("commit with stale version: err=%v, want conflict", err)
	}
	if g.Status != statusWaiting {
		t.Fatalf("gang should be back to waiting, got %s", g.Status)
	}
	for _, id := range []string{"n1", "n2"} {
		if slots := len(s.nodes[id].runningBy) + len(s.nodes[id].reservedBy); slots != 0 {
			t.Fatalf("%s leaked %d slots after failed commit", id, slots)
		}
	}
	assertNoLeaks(t, s)

	_, _, err = s.CommitPlan("g1", p.ID, p.Version)
	if !errors.Is(err, ErrExpired) {
		t.Fatalf("re-commit aborted plan: err=%v, want expired/gone", err)
	}
}

// 5b. Node-level OCC: if a picked node's version changes between reserve and
// commit while it stays online, commit fails conflict and starts nothing.
func TestCommitDetectsChangedNode(t *testing.T) {
	s, _ := newTestScheduler(t)
	mustAddNode(t, s, "n1", "z", 2, nil) // 2 slots so g1 only holds one
	mustAddNode(t, s, "n2", "z", 1, nil)

	g, p, err := s.SubmitGang(gangSpec("g1", 2, nil))
	if err != nil {
		t.Fatal(err)
	}
	if !contains(p.Nodes, "n1") {
		t.Fatalf("expected n1 among picked nodes: %v", p.Nodes)
	}

	// Model any successful concurrent mutation against n1: its version bumps.
	s.mu.Lock()
	s.nodes["n1"].version++
	s.mu.Unlock()

	_, _, err = s.CommitPlan("g1", p.ID, p.Version)
	if !errors.Is(err, ErrConflict) {
		t.Fatalf("commit after node change: err=%v, want conflict", err)
	}
	if g.Status != statusWaiting {
		t.Fatalf("gang = %s, want waiting", g.Status)
	}
	for _, id := range p.Nodes {
		if len(s.nodes[id].runningBy) != 0 {
			t.Fatalf("%s started tasks despite abort", id)
		}
	}
	assertNoLeaks(t, s)
}

// 6. TTL expiry: once the deadline passes the plan is reaped, all slots free
// and the gang moves to expired. A freed node serves the next gang.
func TestReservationTTLExpiry(t *testing.T) {
	s, c := newTestScheduler(t)
	mustAddNode(t, s, "n1", "z", 1, nil)

	g, p, err := s.SubmitGang(gangSpec("g1", 1, nil))
	if err != nil {
		t.Fatal(err)
	}

	// Before the deadline nothing expires.
	c.t = 1_000_000 + 59_999
	if got := s.ExpireDue(c.t); len(got) != 0 {
		t.Fatalf("early expiry: %v", got)
	}
	if g.Status != statusReserved {
		t.Fatalf("gang = %s before deadline, want reserved", g.Status)
	}

	// At/after the deadline: expire and free.
	c.t = 1_000_000 + 60_000
	expired := s.ExpireDue(c.t)
	if len(expired) != 1 || expired[0] != p.ID {
		t.Fatalf("expired = %v, want [%s]", expired, p.ID)
	}
	if g.Status != statusExpired {
		t.Fatalf("gang = %s, want expired", g.Status)
	}
	if len(s.nodes["n1"].reservedBy) != 0 {
		t.Fatalf("n1 still held after ttl: %v", s.nodes["n1"].reservedBy)
	}
	_, _, err = s.CommitPlan("g1", p.ID, p.Version)
	if !errors.Is(err, ErrExpired) {
		t.Fatalf("commit expired plan: err=%v, want expired", err)
	}

	g2, p2, err := s.SubmitGang(gangSpec("g2", 1, nil))
	if err != nil {
		t.Fatal(err)
	}
	if g2.Status != statusReserved || p2 == nil {
		t.Fatalf("g2 = %s/%v, want reserved", g2.Status, p2)
	}
	assertNoLeaks(t, s)
}

// 7. Labels + anti-affinity: tasks spread across distinct label values, and
// selectors narrow candidates; an unsatisfiable spread waits holding nothing.
func TestLabelsAndAntiAffinity(t *testing.T) {
	s, _ := newTestScheduler(t)
	mustAddNode(t, s, "n1", "z1", 1, map[string]string{"disk": "ssd"})
	mustAddNode(t, s, "n2", "z2", 1, map[string]string{"disk": "hdd"})
	mustAddNode(t, s, "n3", "z2", 1, map[string]string{"disk": "ssd"})

	// Two ssd tasks with zone anti-affinity -> n1 (ssd z1) + n3 (ssd z2).
	spec := gangSpec("spread", 2, func(sp *GangSpec) {
		sp.AntiAffinity = "zone"
		sp.Tasks[0].NodeSelector = map[string]string{"disk": "ssd"}
		sp.Tasks[1].NodeSelector = map[string]string{"disk": "ssd"}
	})
	g, p, err := s.SubmitGang(spec)
	if err != nil {
		t.Fatal(err)
	}
	if g.Status != statusReserved {
		t.Fatalf("status = %s, want reserved", g.Status)
	}
	if !equalSet(p.Nodes, []string{"n1", "n3"}) {
		t.Fatalf("picked %v, want n1+n3 (ssd, distinct zone values)", p.Nodes)
	}
	assertNoLeaks(t, s)

	// Two hdd tasks with disk anti-affinity: only one distinct hdd value
	// exists -> impossible -> waits holding nothing.
	impossible := gangSpec("imp", 2, func(sp *GangSpec) {
		sp.AntiAffinity = "disk"
		sp.Tasks[0].NodeSelector = map[string]string{"disk": "hdd"}
		sp.Tasks[1].NodeSelector = map[string]string{"disk": "hdd"}
	})
	g2, p2, err := s.SubmitGang(impossible)
	if err != nil {
		t.Fatal(err)
	}
	if g2.Status != statusWaiting || p2 != nil {
		t.Fatalf("impossible spread = %s/%v, want waiting", g2.Status, p2)
	}
	assertNoLeaks(t, s)
}

// 8. Offlining a node with running tasks is rejected; allowed once the gang
// completes.
func TestOfflineRunningNodeRejected(t *testing.T) {
	s, _ := newTestScheduler(t)
	mustAddNode(t, s, "n1", "z", 1, nil)
	_, p, _ := s.SubmitGang(gangSpec("g1", 1, nil))
	if _, _, err := s.CommitPlan("g1", p.ID, p.Version); err != nil {
		t.Fatal(err)
	}
	err := s.SetNodeOnline("n1", false)
	if !errors.Is(err, ErrConflict) {
		t.Fatalf("offline running node: err=%v, want conflict", err)
	}
	if !s.nodes["n1"].Online {
		t.Fatal("node should remain online")
	}
	if _, err := s.CompleteGang("g1"); err != nil {
		t.Fatal(err)
	}
	if err := s.SetNodeOnline("n1", false); err != nil {
		t.Fatalf("offline after complete: %v", err)
	}
	assertNoLeaks(t, s)
}

// 9. Duplicate commit is rejected. Release frees nodes and retry reserves
// again.
func TestDoubleCommitAndRelease(t *testing.T) {
	s, _ := newTestScheduler(t)
	mustAddNode(t, s, "n1", "z", 1, nil)
	_, p, _ := s.SubmitGang(gangSpec("g1", 1, nil))
	if _, _, err := s.CommitPlan("g1", p.ID, p.Version); err != nil {
		t.Fatal(err)
	}
	if _, _, err := s.CommitPlan("g1", p.ID, p.Version); !errors.Is(err, ErrConflict) {
		t.Fatalf("double commit: err=%v, want conflict", err)
	}

	mustAddNode(t, s, "n2", "z", 1, nil)
	g2, p2, _ := s.SubmitGang(gangSpec("g2", 1, nil))
	if g2.Status != statusReserved {
		t.Fatalf("g2 = %s, want reserved", g2.Status)
	}
	if _, err := s.ReleasePlan("g2", p2.ID); err != nil {
		t.Fatalf("release: %v", err)
	}
	if len(s.nodes[p2.Nodes[0]].reservedBy) != 0 {
		t.Fatal("released plan still holds a slot")
	}
	g2r, p3, err := s.RetryReserve("g2")
	if err != nil {
		t.Fatal(err)
	}
	if g2r.Status != statusReserved || p3 == nil {
		t.Fatalf("retry = %s/%v, want reserved", g2r.Status, p3)
	}
	assertNoLeaks(t, s)
}

// 10. Malformed specs are rejected before touching state.
func TestValidation(t *testing.T) {
	s, _ := newTestScheduler(t)
	cases := []GangSpec{
		{ID: "", MinNodes: 1, Tasks: []TaskSpec{{}}},
		{ID: "g", MinNodes: 0},
		{ID: "g", MinNodes: 2, Tasks: []TaskSpec{{}}},
		{ID: "g", MinNodes: 1, TTLMillis: -1, Tasks: []TaskSpec{{}}},
	}
	for i, sp := range cases {
		if _, _, err := s.SubmitGang(sp); !errors.Is(err, ErrInvalid) {
			t.Fatalf("case %d: err=%v, want invalid", i, err)
		}
	}
	if err := s.AddNode("", "z", nil, 1); !errors.Is(err, ErrInvalid) {
		t.Fatalf("empty node id: err=%v", err)
	}
	if err := s.AddNode("dup", "z", nil, 1); err != nil {
		t.Fatal(err)
	}
	if err := s.AddNode("dup", "z", nil, 1); !errors.Is(err, ErrConflict) {
		t.Fatalf("dup node: err=%v, want conflict", err)
	}
}

// 11. A reserved gang that is invalidated by a node going offline is requeued
// to the front and reserves again the moment capacity returns — ahead of a
// gang submitted in the meantime (FIFO fairness after invalidation).
func TestInvalidatedGangRequeuedAhead(t *testing.T) {
	s, _ := newTestScheduler(t)
	mustAddNode(t, s, "n1", "z", 1, nil)
	mustAddNode(t, s, "n2", "z", 1, nil)

	gA, _, _ := s.SubmitGang(gangSpec("gA", 2, nil))
	if gA.Status != statusReserved {
		t.Fatalf("gA = %s", gA.Status)
	}
	// n2 goes offline: gA invalidated and requeued (still waiting, n1 free).
	if err := s.SetNodeOnline("n2", false); err != nil {
		t.Fatal(err)
	}
	// A later single-node gang can grab the free n1 while gA waits.
	gC, _, err := s.SubmitGang(gangSpec("gC", 1, nil))
	if err != nil {
		t.Fatal(err)
	}
	if gC.Status != statusReserved {
		t.Fatalf("gC = %s, want reserved on n1", gC.Status)
	}
	// n2 returns but n1 is occupied by gC: gA still cannot fit (holds
	// nothing).
	if err := s.SetNodeOnline("n2", true); err != nil {
		t.Fatal(err)
	}
	if gA.Status != statusWaiting {
		t.Fatalf("gA = %s, want waiting", gA.Status)
	}
	// gC finishes, frees n1; gA (requeued ahead) reserves n1+n2 at once.
	pC := s.plans[gC.ActivePlanID]
	if _, _, err := s.CommitPlan("gC", pC.ID, pC.Version); err != nil {
		t.Fatal(err)
	}
	if _, err := s.CompleteGang("gC"); err != nil {
		t.Fatal(err)
	}
	if gA.Status != statusReserved {
		t.Fatalf("gA = %s, want reserved", gA.Status)
	}
	p := s.plans[gA.ActivePlanID]
	if !equalSet(p.Nodes, []string{"n1", "n2"}) {
		t.Fatalf("gA picked %v, want n1+n2", p.Nodes)
	}
	assertNoLeaks(t, s)
}
