package orset

import (
	"fmt"
	"math/rand"
	"reflect"
	"sync"
	"testing"
	"time"
)

func TestAddRemoveLookup(t *testing.T) {
	s := New("r1")
	if s.Lookup("x") {
		t.Fatal("empty set should not contain x")
	}
	tag := s.Add("x")
	if tag == "" {
		t.Fatal("Add returned empty tag")
	}
	if !s.Lookup("x") {
		t.Fatal("x should be present after add")
	}
	tags, _ := s.Remove("x")
	if len(tags) != 1 || tags[0] != tag {
		t.Fatalf("remove should observe the one tag, got %v", tags)
	}
	if s.Lookup("x") {
		t.Fatal("x should be gone after observed remove")
	}
	if got, want := s.Values(), []string{}; !reflect.DeepEqual(got, want) {
		t.Fatalf("values = %v, want empty", got)
	}
}

func TestReAddAfterRemove(t *testing.T) {
	s := New("r1")
	s.Add("x")
	t1, _ := s.Remove("x")
	if len(t1) != 1 {
		t.Fatalf("observed %d tags, want 1", len(t1))
	}
	t2 := s.Add("x") // fresh tag -> element visible again
	if t2 == t1[0] {
		t.Fatal("re-add must mint a NEW unique tag")
	}
	if !s.Lookup("x") {
		t.Fatal("x must be present after re-add; old tombstone must not kill new tag")
	}
}

func TestTagsAreUnique(t *testing.T) {
	s := New("r1")
	seen := map[string]bool{}
	for i := 0; i < 1000; i++ {
		tag := s.Add("e")
		if seen[tag] {
			t.Fatalf("duplicate tag minted: %s", tag)
		}
		seen[tag] = true
	}
}

// TestRemoveOnlyRemovesObserved is the defining OR-Set property: a remove
// tombstones ONLY the tags the remover has observed. A tag added
// concurrently at another replica survives.
func TestRemoveOnlyRemovesObserved(t *testing.T) {
	r1, r2 := New("r1"), New("r2")

	// Both replicas know tag t1 for "a".
	t1 := r1.Add("a")
	r2.Merge(r1.Snapshot())

	// r1 adds "a" again while r2 is offline: r2 never observes t2.
	t2 := r1.Add("a")

	// r2 removes "a": it can only tombstone t1.
	observed, _ := r2.Remove("a")
	if len(observed) != 1 || observed[0] != t1 {
		t.Fatalf("r2 should observe only t1, got %v", observed)
	}

	// Partition heals.
	st1, st2 := r1.Snapshot(), r2.Snapshot()
	r1.Merge(st2)
	r2.Merge(st1)

	// "a" must still be a member: t2 was never observed by the remove.
	if !r1.Lookup("a") || !r2.Lookup("a") {
		t.Fatal("concurrent add must survive observed remove (add-wins)")
	}
	st := r1.Snapshot()
	if !contains(st.Tombstones["a"], t1) {
		t.Fatal("t1 must be tombstoned")
	}
	if contains(st.Tombstones["a"], t2) {
		t.Fatal("t2 must NOT be tombstoned: it was unobserved")
	}

	// Once the new tag is observed, a later remove removes it too.
	r1.Remove("a") // r1 now observes t2 (t1 already dead)
	r2.Merge(r1.Snapshot())
	if r1.Lookup("a") || r2.Lookup("a") {
		t.Fatal("a must be gone after the later observed remove")
	}
}

// TestAddWinsConcurrentRemove is the textbook OR-Set interleaving:
// replica A and B both start with x; A removes x while B concurrently
// re-adds x. After merge x must be present.
func TestAddWinsConcurrentRemove(t *testing.T) {
	a, b := New("a"), New("b")
	a.Add("x")
	b.Merge(a.Snapshot())

	a.Remove("x") // T1: A removes
	b.Add("x")    // T2: B concurrently adds a fresh tag
	a.Merge(b.Snapshot())
	b.Merge(a.Snapshot())

	if !a.Lookup("x") || !b.Lookup("x") {
		t.Fatal("concurrent add must win over concurrent remove")
	}
}

func TestIdempotentMergeAndOps(t *testing.T) {
	a, b := New("a"), New("b")
	a.Add("x")
	a.Add("y")
	st := a.Snapshot()
	// Repeated / duplicate sync merges must change nothing.
	for i := 0; i < 5; i++ {
		b.Merge(st)
	}
	if !reflect.DeepEqual(b.Values(), []string{"x", "y"}) {
		t.Fatalf("values after duplicated merge = %v", b.Values())
	}
	// Duplicated op delivery.
	op := Op{Type: "remove", Element: "x", Tags: st.Add["x"]}
	for i := 0; i < 5; i++ {
		if err := b.ApplyOp(op); err != nil {
			t.Fatal(err)
		}
	}
	if b.Lookup("x") {
		t.Fatal("x should be removed; y should remain")
	}
	if !b.Lookup("y") {
		t.Fatal("y should remain")
	}
}

// TestRemoveBeforeAddArrives proves op-based delivery needs no causal order:
// a remove op delivered BEFORE its matching add still converges to removed.
func TestRemoveBeforeAddArrives(t *testing.T) {
	r := New("r")
	tag := "origin-1-deadbeef"
	if err := r.ApplyOp(Op{Type: "remove", Element: "x", Tags: []string{tag}}); err != nil {
		t.Fatal(err)
	}
	if r.Lookup("x") {
		t.Fatal("x must not appear after remove-only op")
	}
	// The matching add arrives much later (reordered message).
	if err := r.ApplyOp(Op{Type: "add", Element: "x", Tags: []string{tag}}); err != nil {
		t.Fatal(err)
	}
	if r.Lookup("x") {
		t.Fatal("x must NOT resurrect: remove-before-add is stable under reorder")
	}
	// A genuinely new add still wins.
	r.ApplyOp(Op{Type: "add", Element: "x", Tags: []string{"brand-new-tag"}})
	if !r.Lookup("x") {
		t.Fatal("a fresh tag must make x visible again")
	}
}

// ---------- permutation tests ----------

// partitionScenario runs three partitioned replicas through a scripted set of
// mutations with one partial sync, returning:
//   - ops: every mutation as a replicable Op,
//   - snapshots: a post-mutation state snapshot tagged by origin,
//   - origins: final per-origin states,
//
// Final converged members are {w, x, z}.
func partitionScenario(t *testing.T) (ops []Op, snaps []State, a, b, c *ORSet) {
	t.Helper()
	a, b, c = New("A"), New("B"), New("C")
	type event struct {
		origin *ORSet
		op     Op
	}
	var events []event
	add := func(r *ORSet, e string) {
		tag := r.Add(e)
		events = append(events, event{r, Op{Type: "add", Element: e, Tags: []string{tag}}})
	}
	rem := func(r *ORSet, e string) {
		_, op := r.Remove(e)
		events = append(events, event{r, op})
	}
	sync2 := func(x, y *ORSet) {
		sx, sy := x.Snapshot(), y.Snapshot()
		x.Merge(sy)
		y.Merge(sx)
	}

	add(a, "x") // A sees Ax1
	add(a, "y")
	add(b, "x") // B sees Bx1
	add(b, "z")
	add(c, "y")
	add(c, "w")
	sync2(a, c) // A<->C exchange: C learns Ax1/Ay1, A learns Cy1/Cw1
	rem(a, "x") // observes Ax1 only (never saw Bx1)
	rem(c, "y") // observes Ay1,Cy1
	rem(b, "x") // observes Bx1 only
	add(a, "x") // Ax2 fresh tag after remove
	rem(b, "z") // tombstones Bz1
	add(c, "z") // Cz1, concurrent with B's remove -> add wins

	for _, ev := range events {
		if ev.op.Type == "remove" && len(ev.op.Tags) == 0 {
			continue
		}
		ops = append(ops, ev.op)
		snaps = append(snaps, ev.origin.Snapshot())
	}
	return ops, snaps, a, b, c
}

// referenceConvergence is what every replica must end at regardless of
// delivery order. It is computed independently two ways.
func referenceConvergence(t *testing.T, a, b, c *ORSet, ops []Op) State {
	t.Helper()
	// Way 1: union of final origin states (state-based join).
	j := New("join")
	j.Merge(a.Snapshot())
	j.Merge(b.Snapshot())
	j.Merge(c.Snapshot())
	// Way 2: apply every op in script order to a fresh replica (op-based).
	p := New("opref")
	for _, op := range ops {
		if err := p.ApplyOp(op); err != nil {
			t.Fatal(err)
		}
	}
	if !Equal(j.Snapshot(), p.Snapshot()) {
		t.Fatalf("the two reference computations disagree:\njoin=%+v\nops =%+v", j.Snapshot(), p.Snapshot())
	}
	if want := []string{"w", "x", "z"}; !reflect.DeepEqual(j.Values(), want) {
		t.Fatalf("reference values = %v, want %v", j.Values(), want)
	}
	return j.Snapshot()
}

// TestOpDeliveryPermutationsExhaustive exhaustively checks EVERY ordering of
// a 7-op concurrent history (7! = 5040). Every permutation must converge.
func TestOpDeliveryPermutationsExhaustive(t *testing.T) {
	ops := []Op{
		{Type: "add", Element: "x", Tags: []string{"a1"}},
		{Type: "add", Element: "x", Tags: []string{"b1"}},
		{Type: "remove", Element: "x", Tags: []string{"a1"}},
		{Type: "remove", Element: "x", Tags: []string{"b1"}},
		{Type: "add", Element: "y", Tags: []string{"c1"}},
		{Type: "remove", Element: "y", Tags: []string{"c1"}},
		{Type: "add", Element: "x", Tags: []string{"a2"}},
	}
	want := New("want")
	for _, op := range ops {
		if err := want.ApplyOp(op); err != nil {
			t.Fatal(err)
		}
	}
	count := 0
	for _, perm := range permutations(len(ops)) {
		r := New("r")
		for _, i := range perm {
			if err := r.ApplyOp(ops[i]); err != nil {
				t.Fatal(err)
			}
		}
		// Duplicate re-delivery after convergence changes nothing.
		for _, i := range perm {
			_ = r.ApplyOp(ops[i])
		}
		if !Equal(r.Snapshot(), want.Snapshot()) {
			t.Fatalf("permutation %v diverged:\ngot  %+v\nwant %+v", perm, r.Snapshot(), want.Snapshot())
		}
		if !r.Lookup("x") || r.Lookup("y") {
			t.Fatalf("permutation %v wrong members: %v", perm, r.Values())
		}
		count++
	}
	t.Logf("exhaustively verified %d op-delivery permutations", count)
}

// TestOpDeliveryRandomReorders runs the 3-replica partition history through
// many randomized, duplicated, independently-reordered deliveries.
func TestOpDeliveryRandomReorders(t *testing.T) {
	ops, _, a, b, c := partitionScenario(t)
	want := referenceConvergence(t, a, b, c, ops)

	rng := rand.New(rand.NewSource(42))
	const trials = 500
	for trial := 0; trial < trials; trial++ {
		receivers := []*ORSet{New("r1"), New("r2"), New("r3")}
		for _, r := range receivers {
			seq := make([]Op, 0, len(ops)*2)
			seq = append(seq, ops...)
			// Duplicate ~30% of messages (retries / duplicated sync).
			for _, op := range ops {
				if rng.Intn(3) == 0 {
					seq = append(seq, op)
				}
			}
			rng.Shuffle(len(seq), func(i, j int) { seq[i], seq[j] = seq[j], seq[i] })
			for _, op := range seq {
				if err := r.ApplyOp(op); err != nil {
					t.Fatal(err)
				}
			}
		}
		for i, r := range receivers {
			if !Equal(r.Snapshot(), want) {
				t.Fatalf("trial %d receiver %d did not converge:\ngot  %+v\nwant %+v",
					trial, i, r.Snapshot(), want)
			}
		}
	}
	t.Logf("random-reorder op convergence verified over %d trials x 3 receivers", trials)
}

// TestStateDeliveryRandomReorders proves the same for state-based sync:
// post-mutation snapshots are cumulative and may arrive in any order, be
// skipped or duplicated — the final join is identical.
func TestStateDeliveryRandomReorders(t *testing.T) {
	ops, snaps, a, b, c := partitionScenario(t)
	want := referenceConvergence(t, a, b, c, ops)

	rng := rand.New(rand.NewSource(7))
	const trials = 500
	for trial := 0; trial < trials; trial++ {
		r := New("r")
		seq := append([]State(nil), snaps...)
		// duplicate a random third of the snapshots
		for _, idx := range rng.Perm(len(snaps))[:len(snaps)/3] {
			seq = append(seq, snaps[idx])
		}
		rng.Shuffle(len(seq), func(i, j int) { seq[i], seq[j] = seq[j], seq[i] })
		for _, st := range seq {
			r.Merge(st)
		}
		if !Equal(r.Snapshot(), want) {
			t.Fatalf("trial %d state merge diverged:\ngot  %+v\nwant %+v", trial, r.Snapshot(), want)
		}
	}
	t.Logf("random-reorder state convergence verified over %d trials", trials)
}

// TestSemilatticeLaws verifies Merge (join) is commutative, associative and
// idempotent — the mathematical reason all the reorder tests hold.
func TestSemilatticeLaws(t *testing.T) {
	mk := func(seed int64) State {
		r := rand.New(rand.NewSource(seed))
		s := New(fmt.Sprintf("s%d", seed))
		elems := []string{"a", "b", "c", "d"}
		var tagsAdded []string
		for _, e := range elems {
			for k := 0; k < r.Intn(4); k++ {
				tagsAdded = append(tagsAdded, s.Add(e))
			}
		}
		for _, e := range elems {
			if r.Intn(2) == 0 {
				n := r.Intn(len(tagsAdded) + 1)
				st := s.Snapshot()
				if ts := st.Add[e]; len(ts) > 0 && n > 0 {
					if n > len(ts) {
						n = len(ts)
					}
					_ = s.ApplyOp(Op{Type: "remove", Element: e, Tags: ts[:n]})
				}
			}
		}
		return s.Snapshot()
	}
	x, y, z := mk(11), mk(22), mk(33)

	join := func(states ...State) State {
		s := New("j")
		for _, st := range states {
			s.Merge(st)
		}
		return s.Snapshot()
	}
	// commutativity
	if !Equal(join(x, y), join(y, x)) {
		t.Fatal("merge not commutative")
	}
	// associativity
	if !Equal(join(join(x, y), z), join(x, join(y, z))) {
		t.Fatal("merge not associative")
	}
	// idempotence
	if !Equal(join(x, x), x) {
		t.Fatal("merge not idempotent")
	}
}

// TestConcurrentAddRemoveMerge hammers one replica with parallel adds and
// removes while two others concurrently merge snapshots; run with -race.
func TestConcurrentAddRemoveMerge(t *testing.T) {
	s := New("main")
	peers := []*ORSet{New("p1"), New("p2")}
	var wg sync.WaitGroup
	deadline := time.Now().Add(200 * time.Millisecond)

	for w := 0; w < 8; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			rng := rand.New(rand.NewSource(int64(w)))
			for time.Now().Before(deadline) {
				e := fmt.Sprintf("e%d", rng.Intn(6))
				if rng.Intn(2) == 0 {
					s.Add(e)
				} else {
					s.Remove(e)
				}
			}
		}(w)
	}
	for _, p := range peers {
		wg.Add(1)
		go func(p *ORSet) {
			defer wg.Done()
			for time.Now().Before(deadline) {
				p.Merge(s.Snapshot())
			}
		}(p)
	}
	wg.Wait()
	// After a final sync, peers converge to the main state.
	for _, p := range peers {
		p.Merge(s.Snapshot())
		s.Merge(p.Snapshot())
	}
	for _, p := range peers {
		p.Merge(s.Snapshot())
		if !Equal(p.Snapshot(), s.Snapshot()) {
			t.Fatal("peer failed to converge after concurrent workload")
		}
	}
}

// ---------- helpers ----------

func contains(xs []string, v string) bool {
	for _, x := range xs {
		if x == v {
			return true
		}
	}
	return false
}

// permutations returns every permutation of indices [0..n).
func permutations(n int) [][]int {
	var out [][]int
	idx := make([]int, n)
	for i := range idx {
		idx[i] = i
	}
	var rec func(int)
	rec = func(k int) {
		if k == 1 {
			p := append([]int(nil), idx...)
			out = append(out, p)
			return
		}
		rec(k - 1)
		for i := 0; i < k-1; i++ {
			if k%2 == 0 {
				idx[i], idx[k-1] = idx[k-1], idx[i]
			} else {
				idx[0], idx[k-1] = idx[k-1], idx[0]
			}
			rec(k - 1)
		}
	}
	rec(n)
	return out
}
