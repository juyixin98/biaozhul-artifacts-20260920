package orset

import (
	"fmt"
	"reflect"
	"testing"
)

// TestAcceptance_ThreeReplicaEnumeration is the headline acceptance check:
// three replicas add elements, one removes an observed element while another
// add of the same element is concurrent, and every possible ordering of
// merging the three states is enumerated. Every ordering must (1) converge to
// identical states, (2) keep the concurrent add, and (3) tolerate repeated
// and out-of-order state deliveries.
func TestAcceptance_ThreeReplicaEnumeration(t *testing.T) {
	// Local histories before any sync:
	//   R1: add x (tag R1:1), add a (R1:2), then remove x (tombstone R1:1)
	//   R2: add x (tag R2:1) — concurrent with R1's remove; add b
	//   R3: add c, stays an observer of late/duplicated syncs
	build := func() (r1, r2, r3 *State) {
		r1, r2, r3 = New(), New(), New()
		r1.Add("R1", 1, "x")
		r1.Add("R1", 2, "a")
		r1.Remove("x") // observed only R1:1

		r2.Add("R2", 1, "x") // R2 never saw R1's add/remove
		r2.Add("R2", 2, "b")

		r3.Add("R3", 1, "c")
		return r1, r2, r3
	}
	wantValues := []string{"a", "b", "c", "x"} // concurrent x (R2:1) survives
	var r1, r2, r3 *State

	// 1) Enumerate all 6 permutations of merging R1,R2,R3 into an empty sink.
	for _, p := range permutations(3) {
		r1, r2, r3 = build()
		src := []*State{r1, r2, r3}
		sink := New()
		for _, i := range p {
			sink.Merge(src[i])
		}
		if got := sink.Values(); !reflect.DeepEqual(got, wantValues) {
			t.Fatalf("perm %v: values %v want %v", p, got, wantValues)
		}
	}

	// 2) Enumerate pairwise gossip schedules: all 3! pair-orderings, and for
	// each, gossip in both directions, then verify every replica is identical.
	for _, p := range permutations(3) {
		r1, r2, r3 = build()
		nodes := []*State{r1, r2, r3}
		pairs := [][2]int{{p[0], p[1]}, {p[0], p[2]}, {p[1], p[2]}}
		for _, pr := range pairs {
			i, j := pr[0], pr[1]
			nodes[i].Merge(nodes[j])
			nodes[j].Merge(nodes[i])
		}
		for i, n := range nodes {
			if !n.Equal(nodes[0]) {
				t.Fatalf("schedule %v: node %d diverged", p, i)
			}
			if got := n.Values(); !reflect.DeepEqual(got, wantValues) {
				t.Fatalf("schedule %v: values %v want %v", p, got, wantValues)
			}
		}
	}

	// 3) Out-of-order snapshots delivered to R3: an older snapshot that still
	// contains x live arrives AFTER a newer one carrying the tombstone — the
	// element must not resurrect; and the concurrent R2:1 must survive once
	// R2's snapshot shows up (in either order).
	r1, r2, _ = build()
	snapNew := r1.Clone() // {a}, tombstone R1:1
	snapOld := New()
	snapOld.Add("R1", 1, "x")
	snapOld.Add("R1", 2, "a") // {a,x}, no tombstone
	for _, order := range [][]int{{0, 1}, {1, 0}} {
		r3 := New()
		r3.Add("R3", 1, "c") // receiver's local add
		snaps := []*State{snapOld, snapNew}
		r3.Merge(snaps[order[0]])
		r3.Merge(snaps[order[1]])
		r3.Merge(r2) // concurrent add R2:1
		if got := r3.Values(); !reflect.DeepEqual(got, wantValues) {
			t.Fatalf("ooo order %v: values %v want %v (x resurrection: %v)",
				order, got, wantValues, r3.Lookup("x") && hasOnly(r3, "x", "R1", 1))
		}
	}

	// 4) Repeated synchronisation: merging the same source 100 times is
	// identical to merging it once.
	r1, r2, r3 = build()
	once := New().Merge(r1).Merge(r2).Merge(r3)
	rep := New()
	for k := 0; k < 100; k++ {
		rep.Merge(r1).Merge(r2).Merge(r3)
	}
	if !rep.Equal(once) {
		t.Fatal("repeated sync did not converge to the same state as one sync")
	}
	if !reflect.DeepEqual(rep.Values(), wantValues) {
		t.Fatalf("repeated-sync values %v want %v", rep.Values(), wantValues)
	}

	// 5) After everyone observes both x tags, a later remove clears x and
	// propagates by repeated sync — final join drops x, keeps a,b,c.
	join := New().Merge(r1).Merge(r2).Merge(r3)
	join.Remove("x")
	r1.Merge(join)
	for k := 0; k < 3; k++ {
		r2.Merge(r1)
		r3.Merge(r1)
	}
	if r2.Lookup("x") || r3.Lookup("x") {
		t.Fatal("late observed-remove did not propagate through repeated sync")
	}
	if got := join.Values(); !reflect.DeepEqual(got, []string{"a", "b", "c"}) {
		t.Fatalf("post-remove values %v want [a b c]", got)
	}
}

func hasOnly(s *State, elem, origin string, seq uint64) bool {
	for _, t := range s.Adds[elem] {
		if _, dead := s.Removed[t]; !dead && (t.Origin != origin || t.Seq != seq) {
			return false
		}
	}
	return true
}

func permutations(n int) [][]int {
	var out [][]int
	a := make([]int, n)
	for i := range a {
		a[i] = i
	}
	var rec func(int)
	rec = func(k int) {
		if k == 1 {
			out = append(out, append([]int(nil), a...))
			return
		}
		for i := 0; i < k; i++ {
			rec(k - 1)
			if k%2 == 0 {
				a[i], a[k-1] = a[k-1], a[i]
			} else {
				a[0], a[k-1] = a[k-1], a[0]
			}
		}
	}
	rec(n)
	if len(out) != 6 {
		panic(fmt.Sprintf("permutations: got %d", len(out)))
	}
	return out
}
