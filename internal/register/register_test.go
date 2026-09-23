package register

import (
	"fmt"
	"sort"
	"testing"

	"vcreg/internal/vclock"
)

// signature renders a version set as one deterministic string, so two
// replicas have converged exactly when their signatures match.
func signature(vs []Version) string {
	parts := make([]string, len(vs))
	for i, v := range vs {
		parts[i] = fmt.Sprintf("%s@%s", v.Value, v.Clock.Canonical())
	}
	sort.Strings(parts)
	return fmt.Sprint(parts)
}

func sigStore(s *Store, key string) string { return signature(s.Read(key)) }

func TestCausalOverwrite(t *testing.T) {
	s := NewStore("a")
	s.Write("k", "v1", nil)
	s.Write("k", "v2", nil) // later event on same replica, dominates v1
	vs := s.Read("k")
	if len(vs) != 1 || vs[0].Value != "v2" {
		t.Fatalf("causal overwrite failed: %+v", vs)
	}
}

// Acceptance scenario 1: isolated double writes followed by sync must
// converge, and both concurrent versions must survive on both replicas.
func TestPartitionedDoubleWriteThenSync(t *testing.T) {
	const key = "x"
	a := NewStore("a")
	b := NewStore("b")

	// Network partition: each replica writes without seeing the other.
	va := a.Write(key, "from-a", nil)
	vb := b.Write(key, "from-b", nil)
	if vclock.Compare(va.Clock, vb.Clock) != vclock.Concurrent {
		t.Fatalf("expected concurrent clocks, got %v vs %v", va.Clock, vb.Clock)
	}

	// Heal: exchange messages both directions.
	if !b.Receive(key, va) {
		t.Fatal("b should admit a's version")
	}
	if !a.Receive(key, vb) {
		t.Fatal("a should admit b's version")
	}

	for name, s := range map[string]*Store{"a": a, "b": b} {
		vs := s.Read(key)
		if len(vs) != 2 {
			t.Fatalf("%s: want 2 concurrent siblings, got %d (%s)",
				name, len(vs), signature(vs))
		}
		vals := []string{vs[0].Value, vs[1].Value}
		sort.Strings(vals)
		if vals[0] != "from-a" || vals[1] != "from-b" {
			t.Fatalf("%s: siblings = %v", name, vals)
		}
	}
	if sigStore(a, key) != sigStore(b, key) {
		t.Fatalf("replicas did not converge: %s != %s",
			sigStore(a, key), sigStore(b, key))
	}
}

// Acceptance scenario 2: duplicated messages must be harmless.
func TestDuplicateMessage(t *testing.T) {
	const key = "x"
	a := NewStore("a")
	b := NewStore("b")
	va := a.Write(key, "from-a", nil)
	b.Write(key, "from-b", nil)

	// Deliver a->b three times, and the whole b->a snapshot twice.
	if !b.Receive(key, va) {
		t.Fatal("first delivery must be admitted")
	}
	for i := 0; i < 2; i++ {
		if b.Receive(key, va) {
			t.Fatalf("duplicate delivery %d must be rejected", i)
		}
	}
	if got := b.MergeSnapshot(a.Snapshot()); got != 0 {
		t.Fatalf("redundant snapshot admitted %d versions", got)
	}
	if n := a.MergeSnapshot(b.Snapshot()); n != 1 {
		t.Fatalf("first snapshot from b should admit 1 version, got %d", n)
	}
	if n := a.MergeSnapshot(b.Snapshot()); n != 0 {
		t.Fatalf("duplicated snapshot should admit 0 versions, got %d", n)
	}

	if sigStore(a, key) != sigStore(b, key) {
		t.Fatalf("divergence after duplicates: %s != %s",
			sigStore(a, key), sigStore(b, key))
	}
	if len(a.Read(key)) != 2 || len(b.Read(key)) != 2 {
		t.Fatal("duplicate delivery corrupted the sibling set")
	}
}

// Acceptance scenario 3: after an explicit merge, a late old write must be
// pruned rather than resurrected, and the outcome must not depend on message
// order.
func TestLateStaleAfterMerge(t *testing.T) {
	const key = "x"
	oldA := Version{Value: "from-a", Clock: vclock.Clock{"a": 1}, Origin: "a"}
	oldB := Version{Value: "from-b", Clock: vclock.Clock{"b": 1}, Origin: "b"}

	build := func(lateFirst bool) *Store {
		s := NewStore("c")
		incoming := []Version{oldA, oldB}
		if lateFirst {
			// Late message arrives before the resolution is known.
			s.Receive(key, oldA)
		}
		for _, v := range incoming {
			s.Receive(key, v)
		}
		// c observed both siblings and explicitly merges them with context.
		resolved, err := s.Resolve(key, "merged",
			[]vclock.Clock{oldA.Clock, oldB.Clock})
		if err != nil {
			t.Fatal(err)
		}
		// Now the stale a-version is re-delivered (duplicated/late).
		if s.Receive(key, oldA) {
			t.Fatal("late causally-old write must be pruned, not admitted")
		}
		if lateFirst {
			// And an even later re-delivery of the pre-merge b version.
			if s.Receive(key, oldB) {
				t.Fatal("old sibling must not survive re-delivery after merge")
			}
		}
		if vs := s.Read(key); len(vs) != 1 || vs[0].Clock.Canonical() != resolved.Clock.Canonical() {
			t.Fatalf("want exactly the merged version, got %s", signature(vs))
		}
		return s
	}

	s1 := build(false)
	s2 := build(true)
	if sigStore(s1, key) != sigStore(s2, key) {
		t.Fatalf("order dependence: %s vs %s", sigStore(s1, key), sigStore(s2, key))
	}

	// Cross-replica: a only knows the merged result, then stale versions
	// arrive from stragglers; it must still converge to the merge.
	remote := NewStore("c")
	remote.Receive(key, oldA)
	remote.Receive(key, oldB)
	resolved, _ := remote.Resolve(key, "merged",
		[]vclock.Clock{oldA.Clock, oldB.Clock})

	d := NewStore("d")
	d.Receive(key, oldA) // d only saw one side before learning of the merge
	if !d.Receive(key, resolved) {
		t.Fatal("merged version should be admitted")
	}
	if d.Receive(key, oldB) {
		t.Fatal("late old-b must be pruned once merge dominates it")
	}
	vs := d.Read(key)
	if len(vs) != 1 || vs[0].Value != "merged" {
		t.Fatalf("remote did not converge to merged value: %s", signature(vs))
	}
}

// An explicit merge citing two of three concurrent siblings must not delete
// the third sibling the context never covered. It remains concurrent.
func TestResolveKeepsUnrelatedConcurrent(t *testing.T) {
	const key = "k"
	s := NewStore("d")
	vx := Version{Value: "x", Clock: vclock.Clock{"a": 1}}
	vy := Version{Value: "y", Clock: vclock.Clock{"b": 1}}
	vz := Version{Value: "z", Clock: vclock.Clock{"c": 1}}
	for _, v := range []Version{vx, vy, vz} {
		s.Receive(key, v)
	}
	r, err := s.Resolve(key, "xy-merged", []vclock.Clock{vx.Clock, vy.Clock})
	if err != nil {
		t.Fatal(err)
	}
	vs := s.Read(key)
	if len(vs) != 2 {
		t.Fatalf("want merged + untouched sibling (2), got %s", signature(vs))
	}
	rel := vclock.Compare(r.Clock, vz.Clock)
	if rel != vclock.Concurrent {
		t.Fatalf("merge should stay concurrent with unobserved sibling, got %s", rel)
	}
	vals := []string{vs[0].Value, vs[1].Value}
	sort.Strings(vals)
	if vals[0] != "xy-merged" || vals[1] != "z" {
		t.Fatalf("unexpected surviving versions: %v", vals)
	}
}

// Merge commutes with itself: folding peer snapshots in any order yields the
// same antichain.
func TestMergeOrderIndependence(t *testing.T) {
	const key = "k"
	a := NewStore("a")
	b := NewStore("b")
	c := NewStore("c")
	a.Write(key, "1", nil)
	b.Write(key, "2", nil)
	b.Write(key, "2b", nil) // causal overwrite inside b
	c.Write(key, "3", nil)

	order1 := NewStore("t1")
	order1.MergeSnapshot(a.Snapshot())
	order1.MergeSnapshot(b.Snapshot())
	order1.MergeSnapshot(c.Snapshot())

	order2 := NewStore("t2")
	order2.MergeSnapshot(c.Snapshot())
	order2.MergeSnapshot(a.Snapshot())
	order2.MergeSnapshot(b.Snapshot())
	order2.MergeSnapshot(a.Snapshot()) // duplicate, swapped order

	if sigStore(order1, key) != sigStore(order2, key) {
		t.Fatalf("merge is order dependent: %s != %s",
			sigStore(order1, key), sigStore(order2, key))
	}
	if len(order1.Read(key)) != 3 {
		t.Fatalf("want 3 concurrent siblings, got %s", sigStore(order1, key))
	}
}

func TestResolveValidation(t *testing.T) {
	s := NewStore("a")
	s.Write("k", "v", nil)
	bogus := vclock.Clock{"zzz": 99}
	if _, err := s.Resolve("k", "r", []vclock.Clock{bogus}); err != ErrUnknownContext {
		t.Fatalf("want ErrUnknownContext, got %v", err)
	}
	// Empty context resolves against all current siblings.
	r, err := s.Resolve("k", "r", nil)
	if err != nil {
		t.Fatal(err)
	}
	if vs := s.Read("k"); len(vs) != 1 || vs[0].Value != "r" ||
		vclock.Compare(vs[0].Clock, r.Clock) != vclock.Equal {
		t.Fatalf("empty-context resolve failed: %+v", vs)
	}
}

// Three-way full partition, healed via different sync paths, must converge.
func TestThreeReplicaConvergence(t *testing.T) {
	const key = "k"
	nodes := map[string]*Store{}
	for _, id := range []string{"a", "b", "c"} {
		nodes[id] = NewStore(id)
		nodes[id].Write(key, "w-"+id, nil)
	}
	nodes["a"].MergeSnapshot(nodes["b"].Snapshot())
	nodes["c"].MergeSnapshot(nodes["b"].Snapshot())
	nodes["a"].MergeSnapshot(nodes["c"].Snapshot()) // a learns c's own + b's
	nodes["c"].MergeSnapshot(nodes["a"].Snapshot())
	nodes["b"].MergeSnapshot(nodes["c"].Snapshot()) // b catches up last

	want := sigStore(nodes["a"], key)
	for id, s := range nodes {
		got := sigStore(s, key)
		if got != want {
			t.Fatalf("node %s diverged: %s != %s", id, got, want)
		}
		if len(s.Read(key)) != 3 {
			t.Fatalf("node %s: want 3 siblings, got %s", id, got)
		}
	}
}
