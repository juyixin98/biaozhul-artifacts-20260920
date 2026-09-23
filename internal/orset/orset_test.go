package orset

import (
	"encoding/json"
	"math/rand"
	"reflect"
	"sort"
	"testing"
)

func vals(s *State) []string { return s.Values() }

func TestAddRemoveLookup(t *testing.T) {
	s := New()
	if s.Lookup("a") {
		t.Fatal("empty set contains a")
	}
	s.Add("n1", 1, "a")
	if !s.Lookup("a") {
		t.Fatal("a missing after add")
	}
	got := s.Remove("a")
	if len(got) != 1 || got[0] != (Tag{"n1", 1}) {
		t.Fatalf("remove returned %v, want [n1:1]", got)
	}
	if s.Lookup("a") {
		t.Fatal("a present after remove")
	}
	// Removing an absent element is a no-op.
	if r := s.Remove("a"); len(r) != 0 {
		t.Fatalf("second remove = %v, want empty", r)
	}
	if r := s.Remove("ghost"); len(r) != 0 {
		t.Fatalf("remove ghost = %v, want empty", r)
	}
}

func TestReAddAfterRemove(t *testing.T) {
	s := New()
	s.Add("n1", 1, "a")
	s.Remove("a")
	s.Add("n1", 2, "a")
	if !s.Lookup("a") {
		t.Fatal("re-add must make a live again via a new tag")
	}
	if len(vals(s)) != 1 {
		t.Fatalf("values = %v, want [a]", vals(s))
	}
}

// Classic observed-remove scenario: A and B concurrently add the same
// element. A removes having observed only its own tag. After merging, B's
// unobserved concurrent add must survive.
func TestObservedRemoveKeepsConcurrentAdd(t *testing.T) {
	a := New()
	b := New()

	a.Add("A", 1, "x") // A: x tag A:1
	b.Add("B", 1, "x") // B: x tag B:1, concurrent with A's add

	a.Remove("x") // A only knows A:1 → tombstones A:1 only
	if a.Lookup("x") {
		t.Fatal("A should no longer see x")
	}

	c := New().Merge(a).Merge(b)
	if !c.Lookup("x") {
		t.Fatal("concurrent add B:1 was wrongly removed")
	}
	// The merge of in the other direction must behave the same.
	c2 := New().Merge(b).Merge(a)
	if !c2.Lookup("x") || !c.Equal(c2) {
		t.Fatal("merge order changed the result")
	}

	// Once B:1 is also observed, a subsequent remove clears x everywhere.
	c.Remove("x")
	if c.Lookup("x") {
		t.Fatal("x must be gone after observing both tags")
	}
}

// Three replicas with add/delete; enumerate every pairwise-merge ordering and
// check convergence and independence of order.
func TestThreeReplicaMergeOrderEnumeration(t *testing.T) {
	// Build three divergent local histories.
	mk := func() (*State, *State, *State) {
		a, b, c := New(), New(), New()
		a.Add("A", 1, "a")
		a.Add("A", 2, "b")
		// b sees a,b then deletes a; concurrently c adds a.
		b.Merge(a)
		b.Add("B", 1, "c")
		c.Add("C", 1, "a") // concurrent add of a
		b.Remove("a")      // tombstones A:1 only (C:1 unseen)
		c.Add("C", 2, "d")
		return a, b, c
	}

	// Reference join: union everything at once.
	ra, rb, rc := mk()
	want := New().Merge(ra).Merge(rb).Merge(rc)
	wantVals := want.Values()
	if !reflect.DeepEqual(wantVals, []string{"a", "b", "c", "d"}) {
		t.Fatalf("reference values = %v, want [a b c d]", wantVals)
	}

	// Enumerate every interleaving sequence length 6: each step picks one of
	// the 3 ordered pairs (a→b style merges modeled as merge into a running
	// state). Simpler exhaustive check: every permutation of "merge the three
	// states into an empty accumulator".
	states := []*State{nil, nil, nil}
	{
		a, b, c := mk()
		states[0], states[1], states[2] = a, b, c
	}
	perm := [][]int{{0, 1, 2}, {0, 2, 1}, {1, 0, 2}, {1, 2, 0}, {2, 0, 1}, {2, 1, 0}}
	for _, p := range perm {
		acc := New()
		for _, i := range p {
			acc.Merge(states[i])
		}
		if got := acc.Values(); !reflect.DeepEqual(got, wantVals) {
			t.Fatalf("merge permutation %v values = %v, want %v", p, got, wantVals)
		}
		if !acc.Equal(want) {
			t.Fatalf("merge permutation %v produced different state", p)
		}
	}

	// Also enumerate pairwise-gossip merge orders: merge pair AB, then with C,
	// in all orders of the first pair and both directions.
	pairs := [][3]int{
		{0, 1, 2}, {1, 0, 2},
		{0, 2, 1}, {2, 0, 1},
		{1, 2, 0}, {2, 1, 0},
	}
	for _, q := range pairs {
		a, b, c := mk()
		x, y, z := a, b, c
		all := []**State{&x, &y, &z}
		s1, s2, s3 := *all[q[0]], *all[q[1]], *all[q[2]]
		s1.Merge(s2)
		s2.Merge(s1) // bidirectional gossip
		s3.Merge(s1)
		s1.Merge(s3)
		s2.Merge(s3)
		s3.Merge(s2)
		if !s1.Equal(s2) || !s2.Equal(s3) {
			t.Fatalf("pairwise order %v: replicas not equal after gossip", q)
		}
		if got := s1.Values(); !reflect.DeepEqual(got, wantVals) {
			t.Fatalf("pairwise order %v: values %v want %v", q, got, wantVals)
		}
	}
}

// Property-style check: Merge is commutative, associative and idempotent over
// many randomly generated states.
func TestMergeAlgebraRandomized(t *testing.T) {
	rng := rand.New(rand.NewSource(7))
	nodes := []string{"A", "B", "C", "D"}
	randState := func() *State {
		s := New()
		seqs := map[string]uint64{}
		for _, e := range []string{"x", "y", "z", "w"} {
			for k := 0; k < rng.Intn(3); k++ {
				n := nodes[rng.Intn(len(nodes))]
				seqs[n]++
				tag := s.Add(n, seqs[n], e)
				if rng.Intn(2) == 0 {
					// Can only tombstone tags this state actually added here
					// (its own tags are by definition observed).
					s.Removed[tag] = struct{}{}
				}
			}
		}
		return s
	}
	for iter := 0; iter < 200; iter++ {
		a, b, c := randState(), randState(), randState()

		// idempotent: a ⊔ a = a
		ai := a.Clone().Merge(a)
		if !ai.Equal(a) {
			t.Fatalf("iter %d: merge not idempotent", iter)
		}

		// commutative: a ⊔ b = b ⊔ a
		ab := a.Clone().Merge(b)
		ba := b.Clone().Merge(a)
		if !ab.Equal(ba) {
			t.Fatalf("iter %d: merge not commutative", iter)
		}

		// associative: (a ⊔ b) ⊔ c = a ⊔ (b ⊔ c)
		l := a.Clone().Merge(b).Merge(c)
		r := a.Clone().Merge(b.Clone().Merge(c))
		if !l.Equal(r) {
			t.Fatalf("iter %d: merge not associative", iter)
		}
	}
}

// Duplicate synchronisation must be a no-op after the first delivery.
func TestRepeatedSyncIsIdempotent(t *testing.T) {
	a := New()
	a.Add("A", 1, "x")
	a.Add("A", 2, "y")
	b := New()
	before := b.Clone()
	for i := 0; i < 5; i++ {
		b.Merge(a)
	}
	if !b.Equal(New().Merge(before).Merge(a)) {
		t.Fatal("repeated identical sync changed state")
	}
	if got := b.Values(); len(got) != 2 {
		t.Fatalf("values = %v, want [x y]", got)
	}
	// Tombstones propagate the same way.
	a.Remove("x")
	for i := 0; i < 3; i++ {
		b.Merge(a)
	}
	if b.Lookup("x") || !b.Lookup("y") {
		t.Fatalf("tombstone sync wrong: values %v", b.Values())
	}
}

func TestJSONRoundTrip(t *testing.T) {
	s := New()
	s.Add("n1", 1, "a")
	s.Add("n1", 2, "a")
	s.Add("n2", 1, "b")
	s.Remove("a") // tombstones n1:1, n1:2

	raw, err := json.Marshal(s)
	if err != nil {
		t.Fatal(err)
	}
	var got State
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatal(err)
	}
	if !got.Equal(s) {
		t.Fatalf("round trip mismatch:\n%s\nwant %s", got.Canonical(), s.Canonical())
	}
	if !reflect.DeepEqual(got.Values(), []string{"b"}) {
		t.Fatalf("values = %v, want [b]", got.Values())
	}
	// Sorted, deterministic wire form for removed tags.
	var anyJSON map[string]any
	_ = json.Unmarshal(raw, &anyJSON)
	rem, _ := anyJSON["removed"].([]any)
	if !reflect.DeepEqual(rem, []any{"n1:1", "n1:2"}) {
		t.Fatalf("removed = %v, want sorted [n1:1 n1:2]", rem)
	}
}

// Coordinated reclamation after all replicas observed tag+tombstone must
// preserve query results; reclaiming with an uninformed witness must be
// refused (stability precondition).
func TestReclaimStability(t *testing.T) {
	a, b, c := New(), New(), New()
	a.Add("A", 1, "x")
	b.Merge(a)
	c.Merge(a)
	a.Remove("x")
	// b,c observe the tombstone.
	b.Merge(a)
	c.Merge(a)

	if a.Lookup("x") || b.Lookup("x") || c.Lookup("x") {
		t.Fatal("x should be removed everywhere")
	}

	// Unstable: B has not observed the tombstone yet. Reclaim on A must be
	// refused even though A itself is fully tombstoned — reclaiming now and
	// later merging with B (which knows the tag but not the removal) would
	// resurrect x.
	pre := a.Clone()
	bStale := New() // a node that knows the tag but NOT the tombstone
	bStale.Add("A", 1, "x")
	if n := a.Reclaim([]Tag{{"A", 1}}, a, bStale, c); n != 0 {
		t.Fatalf("reclaimed %d tags while a witness lacked the tombstone", n)
	}
	if !a.Equal(pre) {
		t.Fatal("state changed after unsafe reclaim attempt")
	}

	// Stable: all three know tag and tombstone → safe on all replicas.
	// The barrier decision uses pre-reclaim snapshots: once A compacts itself,
	// it no longer contains the tombstone that B needs as evidence.
	tags := StableTags("x", a, b, c)
	if len(tags) != 1 {
		t.Fatalf("stable tags = %v, want [A:1]", tags)
	}
	witnessA, witnessB, witnessC := a.Clone(), b.Clone(), c.Clone()
	for _, s := range []*State{a, b, c} {
		if n := s.Reclaim(tags, witnessA, witnessB, witnessC); n != 1 {
			t.Fatalf("reclaim returned %d, want 1", n)
		}
		if s.Lookup("x") {
			t.Fatal("query changed after safe reclaim")
		}
	}
	if !a.Equal(b) || !b.Equal(c) {
		t.Fatal("replicas diverged after GC")
	}
	if len(a.Tombstones()) != 0 || len(a.Adds) != 0 {
		t.Fatalf("state not compacted: %s", a.Canonical())
	}
}

func TestValuesSorted(t *testing.T) {
	s := New()
	for _, e := range []string{"c", "a", "b"} {
		s.Add("n", uint64(e[0]), e)
	}
	if got := s.Values(); !sort.StringsAreSorted(got) || len(got) != 3 {
		t.Fatalf("values = %v", got)
	}
}
