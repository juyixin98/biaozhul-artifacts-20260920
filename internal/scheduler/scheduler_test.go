package scheduler

import (
	"strings"
	"sync"
	"testing"
	"time"
)

func testScheduler(t *testing.T, ttl, reap time.Duration) *Scheduler {
	t.Helper()
	s := New(Config{DefaultTTL: ttl, ReapEvery: reap})
	t.Cleanup(s.Stop)
	return s
}

func mustNode(t *testing.T, s *Scheduler, name string, cap int, labels map[string]string) {
	t.Helper()
	if err := s.AddNode(name, labels, cap); err != nil {
		t.Fatalf("AddNode(%s): %v", name, err)
	}
}

func gangStatus(s *Scheduler, id string) string {
	g, err := s.GetGang(id)
	if err != nil {
		return ""
	}
	return g.Status
}

// assertInvariants checks the never-leak / no-double-book accounting:
//   - 0 <= reserved, 0 <= running, reserved+running <= capacity per node
//   - sum(node reserved) == sum of slots over all HELD reservations
func assertInvariants(t *testing.T, s *Scheduler) {
	t.Helper()
	st := s.Snapshot()
	heldTotal := map[string]int{}
	for _, g := range st.Gangs {
		if g.Reservation != nil {
			if g.Status != GangHeld {
				t.Errorf("gang %s has reservation but status=%s", g.ID, g.Status)
			}
			for _, a := range g.Reservation.Assignments {
				heldTotal[a.NodeName] += a.Slots
			}
		}
		if g.Status == GangHeld && g.Reservation == nil {
			t.Errorf("gang %s HELD without reservation", g.ID)
		}
	}
	for _, n := range st.Nodes {
		if n.Reserved < 0 || n.Running < 0 {
			t.Errorf("node %s negative accounting: reserved=%d running=%d", n.Name, n.Reserved, n.Running)
		}
		if n.Reserved+n.Running > n.Capacity {
			t.Errorf("node %s overcommitted: reserved=%d running=%d capacity=%d",
				n.Name, n.Reserved, n.Running, n.Capacity)
		}
		if n.Reserved != heldTotal[n.Name] {
			t.Errorf("node %s reserved=%d but HELD reservations account for %d",
				n.Name, n.Reserved, heldTotal[n.Name])
		}
	}
}

func TestAllOrNothingHoldsAllTasks(t *testing.T) {
	s := testScheduler(t, time.Second, time.Hour)
	mustNode(t, s, "n1", 4, nil)
	mustNode(t, s, "n2", 4, nil)

	// 6 slots over 2 nodes fits; 9 slots does not.
	g, err := s.SubmitGang("g-fit", []TaskSpec{
		{ID: "t1", Slots: 3}, {ID: "t2", Slots: 3},
	}, false, 0)
	if err != nil {
		t.Fatal(err)
	}
	if g.Status != GangHeld || len(g.Reservation.Assignments) != 2 {
		t.Fatalf("expected HELD with 2 assignments, got %+v", g)
	}

	g2, err := s.SubmitGang("g-big", []TaskSpec{
		{ID: "t1", Slots: 5}, {ID: "t2", Slots: 4},
	}, false, 0)
	if err != nil {
		t.Fatal(err)
	}
	if g2.Status != GangWaiting {
		t.Fatalf("expected WAITING, got %s", g2.Status)
	}
	assertInvariants(t, s)
}

func TestNodeLabelsMatchAndIN(t *testing.T) {
	s := testScheduler(t, time.Second, time.Hour)
	mustNode(t, s, "gpu-a", 8, map[string]string{"zone": "a", "tier": "gpu"})
	mustNode(t, s, "gpu-b", 8, map[string]string{"zone": "b", "tier": "gpu"})
	mustNode(t, s, "cpu-a", 8, map[string]string{"zone": "a", "tier": "cpu"})

	g, err := s.SubmitGang("g1", []TaskSpec{
		{ID: "x", Slots: 1, Match: map[string]string{"tier": "gpu"}, In: map[string][]string{"zone": {"a", "b"}}},
	}, false, 0)
	if err != nil {
		t.Fatal(err)
	}
	if g.Reservation.Assignments[0].NodeName != "gpu-a" &&
		g.Reservation.Assignments[0].NodeName != "gpu-b" {
		t.Fatalf("expected gpu node, got %s", g.Reservation.Assignments[0].NodeName)
	}

	// Unsatisfiable selector -> WAITING, nothing held.
	g2, _ := s.SubmitGang("g2", []TaskSpec{{ID: "y", Slots: 1, Match: map[string]string{"tier": "tpu"}}}, false, 0)
	if g2.Status != GangWaiting {
		t.Fatalf("expected WAITING for tpu, got %s", g2.Status)
	}
	assertInvariants(t, s)
}

func TestDistinctNodesAntiAffinity(t *testing.T) {
	s := testScheduler(t, time.Second, time.Hour)
	mustNode(t, s, "big", 10, nil)

	// Two tasks, one node, distinct required -> must WAIT (no partial hold).
	g, _ := s.SubmitGang("g", []TaskSpec{
		{ID: "t1", Slots: 1}, {ID: "t2", Slots: 1},
	}, true, 0)
	if g.Status != GangWaiting {
		t.Fatalf("expected WAITING with distinct on one node, got %s", g.Status)
	}

	mustNode(t, s, "big2", 10, nil)
	// Adding a node pumps the FIFO queue, so g is promoted automatically.
	g2, err := s.GetGang("g")
	if err != nil {
		t.Fatal(err)
	}
	if g2.Status != GangHeld {
		t.Fatalf("expected HELD after second node added, got %s", g2.Status)
	}
	nodes := map[string]bool{}
	for _, a := range g2.Reservation.Assignments {
		if nodes[a.NodeName] {
			t.Fatalf("node %s used twice under distinct constraint", a.NodeName)
		}
		nodes[a.NodeName] = true
	}
	assertInvariants(t, s)
}

// TestCompetingGangsOverlappingNodes is acceptance scenario A: two gangs
// compete for overlapping capacity. Only one can hold at a time; when the
// first commits and releases, the second is promoted atomically.
func TestCompetingGangsOverlappingNodes(t *testing.T) {
	s := testScheduler(t, 5*time.Second, time.Hour)
	mustNode(t, s, "n1", 6, nil)
	mustNode(t, s, "n2", 6, nil)

	// gang A: 4+4 = 8, gang B: 4+4 = 8; total free = 12, cannot both fit.
	a, err := s.SubmitGang("A", []TaskSpec{
		{ID: "a1", Slots: 4}, {ID: "a2", Slots: 4},
	}, false, 0)
	if err != nil {
		t.Fatal(err)
	}
	b, err := s.SubmitGang("B", []TaskSpec{
		{ID: "b1", Slots: 4}, {ID: "b2", Slots: 4},
	}, false, 0)
	if err != nil {
		t.Fatal(err)
	}
	if a.Status != GangHeld {
		t.Fatalf("A should hold, got %s", a.Status)
	}
	if b.Status != GangWaiting {
		t.Fatalf("B should wait, got %s", b.Status)
	}
	assertInvariants(t, s)

	// Commit A with observed versions.
	if _, err := s.Commit("A", a.Reservation.NodeVersions); err != nil {
		t.Fatalf("commit A: %v", err)
	}
	if got := gangStatus(s, "B"); got != GangWaiting {
		t.Fatalf("B must stay WAITING while A runs, got %s", got)
	}

	// Release A: B must be promoted in full, atomically.
	if _, err := s.ReleaseGang("A"); err != nil {
		t.Fatal(err)
	}
	if got := gangStatus(s, "B"); got != GangHeld {
		t.Fatalf("B should be promoted to HELD, got %s", got)
	}
	b2, _ := s.GetGang("B")
	if len(b2.Reservation.Assignments) != 2 {
		t.Fatalf("B must hold BOTH tasks, got %+v", b2.Reservation)
	}
	if _, err := s.Commit("B", b2.Reservation.NodeVersions); err != nil {
		t.Fatalf("commit B: %v", err)
	}
	assertInvariants(t, s)
}

// TestNodeDownBetweenReserveAndCommit is the headline acceptance scenario:
// take a planned node OFFLINE in the reserve->commit window. Commit must be
// rejected, the whole gang must FAILED (no partial start), and every held
// slot must be released (no leak).
func TestNodeDownBetweenReserveAndCommit(t *testing.T) {
	s := testScheduler(t, 5*time.Second, time.Hour)
	mustNode(t, s, "n1", 4, nil)
	mustNode(t, s, "n2", 4, nil)

	g, err := s.SubmitGang("G", []TaskSpec{
		{ID: "t1", Slots: 2}, {ID: "t2", Slots: 2},
	}, false, 0)
	if err != nil {
		t.Fatal(err)
	}
	if g.Status != GangHeld {
		t.Fatalf("expected HELD, got %s", g.Status)
	}
	staleVersions := g.Reservation.NodeVersions

	// Node goes down before the client commits.
	downed := g.Reservation.Assignments[0].NodeName
	if err := s.SetNodeStatus(downed, NodeOffline); err != nil {
		t.Fatal(err)
	}

	// Commit with the old versions: VERSION_MISMATCH, gang FAILED, no running.
	_, err = s.Commit("G", staleVersions)
	var ce *ConflictError
	if !asConflict(err, &ce) || ce.Code != "VERSION_MISMATCH" {
		t.Fatalf("expected VERSION_MISMATCH, got %v", err)
	}
	after, _ := s.GetGang("G")
	if after.Status != GangFailed {
		t.Fatalf("expected FAILED, got %s (%s)", after.Status, after.Reason)
	}
	if len(after.Running) != 0 {
		t.Fatalf("no task may be running after failed commit: %+v", after.Running)
	}
	for _, n := range s.Snapshot().Nodes {
		if n.Running != 0 || n.Reserved != 0 {
			t.Fatalf("leak on %s: reserved=%d running=%d", n.Name, n.Reserved, n.Running)
		}
	}

	// A second commit attempt must not succeed or double-allocate.
	if _, err := s.Commit("G", staleVersions); err == nil {
		t.Fatal("second commit on failed gang must fail")
	}
	assertInvariants(t, s)

	// Bring node back; released capacity lets a replan succeed.
	if err := s.SetNodeStatus(downed, NodeOnline); err != nil {
		t.Fatal(err)
	}
	g2, err := s.Replan("G")
	if err != nil {
		t.Fatal(err)
	}
	if g2.Status != GangHeld {
		t.Fatalf("expected HELD after replan, got %s (%s)", g2.Status, g2.Reason)
	}
	if _, err := s.Commit("G", g2.Reservation.NodeVersions); err != nil {
		t.Fatalf("commit after replan: %v", err)
	}
	assertInvariants(t, s)
}

// TestCommitRechecksEvenWithMatchingVersions forces the non-version
// revalidation path: a node's version is unchanged in the map but capacity
// reality differs (simulated by tampering-free scenario via offline of a
// node not in the version map is impossible; instead assert a plan missing a
// planned node from versions is rejected as BAD REQUEST without changing
// state).
func TestCommitRequiresAllPlannedVersions(t *testing.T) {
	s := testScheduler(t, time.Second, time.Hour)
	mustNode(t, s, "n1", 4, nil)
	mustNode(t, s, "n2", 4, nil)
	g, _ := s.SubmitGang("G", []TaskSpec{{ID: "t1", Slots: 1}, {ID: "t2", Slots: 1}}, false, 0)
	versions := map[string]int64{}
	for k, v := range g.Reservation.NodeVersions {
		versions[k] = v
	}
	// Drop one entry.
	for k := range versions {
		delete(versions, k)
		break
	}
	_, err := s.Commit("G", versions)
	var ve *ValidationError
	if !asValidation(err, &ve) {
		t.Fatalf("expected validation error, got %v", err)
	}
	if gangStatus(s, "G") != GangHeld {
		t.Fatal("gang must stay HELD after a malformed commit request")
	}
	assertInvariants(t, s)
}

func TestReservationTTLExpiryFreesSlots(t *testing.T) {
	s := testScheduler(t, 60*time.Millisecond, 10*time.Millisecond)
	mustNode(t, s, "n1", 4, nil)
	mustNode(t, s, "n2", 4, nil)

	g, _ := s.SubmitGang("A", []TaskSpec{{ID: "a1", Slots: 4}, {ID: "a2", Slots: 4}}, false, 60*time.Millisecond)
	if g.Status != GangHeld {
		t.Fatal(g.Status)
	}
	// B queued behind A. Its own TTL is long; only its reservation window
	// (started at promotion) needs to outlive the test.
	b, _ := s.SubmitGang("B", []TaskSpec{{ID: "b1", Slots: 2}}, false, 5*time.Second)
	if b.Status != GangWaiting {
		t.Fatal(b.Status)
	}

	time.Sleep(150 * time.Millisecond)

	a2, _ := s.GetGang("A")
	if a2.Status != GangFailed || !strings.Contains(a2.Reason, "RESERVATION_EXPIRED") {
		t.Fatalf("A should expire as FAILED, got %s %s", a2.Status, a2.Reason)
	}
	// B must have been promoted by the reaper pump.
	b2, _ := s.GetGang("B")
	if b2.Status != GangHeld {
		t.Fatalf("B should be promoted after A expiry, got %s", b2.Status)
	}
	assertInvariants(t, s)
}

func TestWaitingQueueIsFIFO(t *testing.T) {
	s := testScheduler(t, time.Hour, time.Hour)
	mustNode(t, s, "n1", 2, nil)

	// First-come holds everything.
	first, _ := s.SubmitGang("first", []TaskSpec{{ID: "t", Slots: 2}}, false, 0)
	if first.Status != GangHeld {
		t.Fatal(first.Status)
	}
	second, _ := s.SubmitGang("second", []TaskSpec{{ID: "t", Slots: 1}}, false, 0)
	third, _ := s.SubmitGang("third", []TaskSpec{{ID: "t", Slots: 2}}, false, 0)
	// second fits behind first but head-of-line: third is ahead of it? No —
	// FIFO: second arrived before third and fits, so it gets HELD at submit
	// time via the pump (2 total cap, first holds 2 -> second WAITS too).
	if second.Status != GangWaiting || third.Status != GangWaiting {
		t.Fatalf("second=%s third=%s", second.Status, third.Status)
	}
	// Commit first then release: second (1 slot) must be promoted before third.
	if _, err := s.Commit("first", first.Reservation.NodeVersions); err != nil {
		t.Fatal(err)
	}
	if _, err := s.ReleaseGang("first"); err != nil {
		t.Fatal(err)
	}
	if gangStatus(s, "second") != GangHeld {
		t.Fatalf("second must be promoted first (FIFO), got %s", gangStatus(s, "second"))
	}
	if gangStatus(s, "third") != GangWaiting {
		t.Fatalf("third must still wait, got %s", gangStatus(s, "third"))
	}
	assertInvariants(t, s)
}

func TestReleaseCancelsWaitingAndFreesQueue(t *testing.T) {
	s := testScheduler(t, time.Hour, time.Hour)
	mustNode(t, s, "n1", 2, nil)
	first, _ := s.SubmitGang("first", []TaskSpec{{ID: "t", Slots: 2}}, false, 0)
	second, _ := s.SubmitGang("second", []TaskSpec{{ID: "t", Slots: 2}}, false, 0)
	if first.Status != GangHeld || second.Status != GangWaiting {
		t.Fatalf("first=%s second=%s", first.Status, second.Status)
	}
	if _, err := s.ReleaseGang("first"); err != nil {
		t.Fatal(err)
	}
	if gangStatus(s, "second") != GangHeld {
		t.Fatalf("second should be promoted, got %s", gangStatus(s, "second"))
	}
	assertInvariants(t, s)
}

// TestConcurrentClientsNoDoubleBook hammers reserve/commit/release from many
// goroutines with -race coverage; accounting invariants must always hold.
func TestConcurrentClientsNoDoubleBook(t *testing.T) {
	s := testScheduler(t, time.Hour, 5*time.Millisecond)
	for i := 0; i < 4; i++ {
		mustNode(t, s, nodeName(i), 4, nil)
	}
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			id := "g" + itoa(i)
			g, err := s.SubmitGang(id, []TaskSpec{{ID: "t", Slots: 1}}, false, 0)
			if err != nil {
				t.Errorf("submit %s: %v", id, err)
				return
			}
			if g.Status == GangHeld {
				if _, err := s.Commit(id, g.Reservation.NodeVersions); err != nil {
					t.Errorf("commit %s: %v", id, err)
					return
				}
				time.Sleep(time.Millisecond)
				if _, err := s.ReleaseGang(id); err != nil {
					t.Errorf("release %s: %v", id, err)
				}
			}
		}(i)
	}
	wg.Wait()
	assertInvariants(t, s)

	// Drain gangs that were promoted to HELD after their client goroutine
	// exited (bounded by TTL in production; released explicitly here).
	for _, g := range s.Snapshot().Gangs {
		if g.Status == GangHeld || g.Status == GangWaiting {
			if _, err := s.ReleaseGang(g.ID); err != nil {
				t.Fatalf("cleanup release %s: %v", g.ID, err)
			}
		}
	}
	assertInvariants(t, s)

	// Every node must be fully drained at the end (no leak).
	for _, n := range s.Snapshot().Nodes {
		if n.Reserved != 0 || n.Running != 0 {
			t.Fatalf("node %s leaked: reserved=%d running=%d", n.Name, n.Reserved, n.Running)
		}
	}
}

func TestOfflineNodeWhileHeldDoesNotLeakAndCommitFails(t *testing.T) {
	s := testScheduler(t, time.Hour, time.Hour)
	mustNode(t, s, "n1", 2, nil)
	g, _ := s.SubmitGang("g", []TaskSpec{{ID: "t", Slots: 2}}, false, 0)
	if err := s.SetNodeStatus("n1", NodeOffline); err != nil {
		t.Fatal(err)
	}
	// Commit with fresh map from the reservation snapshot: the version moved.
	_, err := s.Commit("g", g.Reservation.NodeVersions)
	var ce *ConflictError
	if !asConflict(err, &ce) || ce.Code != "VERSION_MISMATCH" {
		t.Fatalf("expected VERSION_MISMATCH, got %v", err)
	}
	st := s.Snapshot()
	for _, n := range st.Nodes {
		if n.Reserved != 0 {
			t.Fatalf("held slots must be released: %s reserved=%d", n.Name, n.Reserved)
		}
	}
	assertInvariants(t, s)
}

func nodeName(i int) string {
	return "n" + itoa(i+1)
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	neg := i < 0
	if neg {
		i = -i
	}
	var b [20]byte
	p := len(b)
	for i > 0 {
		p--
		b[p] = byte('0' + i%10)
		i /= 10
	}
	if neg {
		p--
		b[p] = '-'
	}
	return string(b[p:])
}

func asConflict(err error, target **ConflictError) bool {
	for err != nil {
		if c, ok := err.(*ConflictError); ok {
			*target = c
			return true
		}
		type unwrapper interface{ Unwrap() error }
		u, ok := err.(unwrapper)
		if !ok {
			return false
		}
		err = u.Unwrap()
	}
	return false
}

func asValidation(err error, target **ValidationError) bool {
	if c, ok := err.(*ValidationError); ok {
		*target = c
		return true
	}
	return false
}
