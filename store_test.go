package main

import (
	"reflect"
	"sort"
	"testing"
)

func newTestStore(t *testing.T, ids ...string) *Store {
	t.Helper()
	s := NewStore()
	for _, id := range ids {
		if err := s.CreateReplica(id); err != nil {
			t.Fatalf("create replica %q: %v", id, err)
		}
	}
	return s
}

func readIDs(t *testing.T, s *Store, replica, key string) []string {
	t.Helper()
	view, err := s.Read(replica, key)
	if err != nil {
		t.Fatalf("read %s/%s: %v", replica, key, err)
	}
	return ids(view.Versions)
}

func TestStore_IsolatedWritesThenSync(t *testing.T) {
	s := newTestStore(t, "A", "B")
	k := "cfg"

	vA, _, err := s.Write("A", k, "value-from-A")
	if err != nil {
		t.Fatal(err)
	}
	vB, _, err := s.Write("B", k, "value-from-B")
	if err != nil {
		t.Fatal(err)
	}

	// Isolation: neither replica has seen the other's write.
	if got := readIDs(t, s, "A", k); !reflect.DeepEqual(got, []string{vA.ID}) {
		t.Fatalf("A before sync = %v", got)
	}
	if got := readIDs(t, s, "B", k); !reflect.DeepEqual(got, []string{vB.ID}) {
		t.Fatalf("B before sync = %v", got)
	}

	// Two-way exchange: both converge to the two concurrent siblings.
	if _, err := s.Sync("A", "B", SyncModeTwoWay); err != nil {
		t.Fatal(err)
	}
	want := []string{vA.ID, vB.ID}
	if got := readIDs(t, s, "A", k); !reflect.DeepEqual(got, want) {
		t.Fatalf("A after sync = %v, want %v", got, want)
	}
	if got := readIDs(t, s, "B", k); !reflect.DeepEqual(got, want) {
		t.Fatalf("B after sync = %v, want %v", got, want)
	}
}

func TestStore_DuplicateMessageDelivery(t *testing.T) {
	s := newTestStore(t, "A", "B")
	k := "cfg"
	vA, _, err := s.Write("A", k, "x")
	if err != nil {
		t.Fatal(err)
	}

	for i := 0; i < 3; i++ {
		res, err := s.Deliver("B", k, []Version{vA})
		if err != nil {
			t.Fatal(err)
		}
		want := OutcomeAccepted
		if i > 0 {
			want = OutcomeDuplicate
		}
		if got := res.Outcomes[vA.ID]; got != want {
			t.Fatalf("delivery #%d outcome = %v, want %v", i+1, got, want)
		}
	}
	if got := readIDs(t, s, "B", k); !reflect.DeepEqual(got, []string{vA.ID}) {
		t.Fatalf("B stored %d copies of the same message: %v", len(got), got)
	}

	// Sync over the same versions must likewise report duplicates, not copies.
	report, err := s.Sync("A", "B", SyncModeOneWay)
	if err != nil {
		t.Fatal(err)
	}
	if len(report.Outbound) != 1 || report.Outbound[0].Duplicate != 1 {
		t.Fatalf("resync report = %+v, want 1 duplicate", report)
	}
}

func TestStore_ExplicitMergeThenLateStaleWrite(t *testing.T) {
	s := newTestStore(t, "A", "B")
	k := "cfg"

	vA, _, _ := s.Write("A", k, "a")
	vB, _, _ := s.Write("B", k, "b")
	if _, err := s.Sync("A", "B", SyncModeTwoWay); err != nil {
		t.Fatal(err)
	}

	// A explicitly joins BOTH siblings into a merged value.
	merged, survivors, err := s.Merge("A", k, "a+b", []string{vA.ID, vB.ID})
	if err != nil {
		t.Fatal(err)
	}
	if got := ids(survivors); !reflect.DeepEqual(got, []string{merged.ID}) {
		t.Fatalf("after merge A holds %v, want only [%s]", got, merged.ID)
	}
	if CompareClocks(merged.Clock, vA.Clock) != ClockAfter ||
		CompareClocks(merged.Clock, vB.Clock) != ClockAfter {
		t.Fatalf("merge clock %v does not dominate both siblings", merged.Clock)
	}

	// Propagate the merge to B.
	if _, err := s.Sync("A", "B", SyncModeOneWay); err != nil {
		t.Fatal(err)
	}
	if got := readIDs(t, s, "B", k); !reflect.DeepEqual(got, []string{merged.ID}) {
		t.Fatalf("B after merge sync = %v", got)
	}

	// A late, stale message carrying the pre-merge sibling vB arrives at B
	// (a delayed/reordered packet). It must be rejected as superseded and
	// must not resurrect the old version.
	res, err := s.Deliver("B", k, []Version{vB})
	if err != nil {
		t.Fatal(err)
	}
	if res.Outcomes[vB.ID] != OutcomeSuperseded {
		t.Fatalf("late stale write outcome = %v, want superseded", res.Outcomes[vB.ID])
	}
	if got := readIDs(t, s, "B", k); !reflect.DeepEqual(got, []string{merged.ID}) {
		t.Fatalf("stale write resurrected a sibling: %v", got)
	}
}

func TestStore_MergeKeepsConcurrentOutsider(t *testing.T) {
	s := newTestStore(t, "A", "B", "C")
	k := "cfg"

	vA, _, _ := s.Write("A", k, "a")
	vB, _, _ := s.Write("B", k, "b")
	if _, err := s.Sync("A", "B", SyncModeTwoWay); err != nil {
		t.Fatal(err)
	}
	// C is isolated and writes without seeing A or B.
	vC, _, _ := s.Write("C", k, "c")

	// A merges only A's and B's siblings.
	merged, _, err := s.Merge("A", k, "a+b", []string{vA.ID, vB.ID})
	if err != nil {
		t.Fatal(err)
	}

	// Bring everything together: the merge must NOT delete C's concurrent
	// version; C must NOT delete the merge either.
	if _, err := s.Sync("A", "C", SyncModeTwoWay); err != nil {
		t.Fatal(err)
	}
	for _, replica := range []string{"A", "C"} {
		got := readIDs(t, s, replica, k)
		want := []string{merged.ID, vC.ID}
		sort.Strings(want)
		if !reflect.DeepEqual(got, want) {
			t.Fatalf("%s = %v, want %v (merge must not erase a concurrent version)",
				replica, got, want)
		}
	}
}

func TestStore_PlainWriteOverwriteChain(t *testing.T) {
	s := newTestStore(t, "A")
	v1, _, _ := s.Write("A", "k", "1")
	v2, survivors, _ := s.Write("A", "k", "2")
	if got := ids(survivors); !reflect.DeepEqual(got, []string{v2.ID}) {
		t.Fatalf("second write survivors = %v", got)
	}
	if CompareClocks(v2.Clock, v1.Clock) != ClockAfter {
		t.Fatalf("v2 %v should dominate v1 %v", v2.Clock, v1.Clock)
	}
}

func TestStore_MergeMissingContextVersion(t *testing.T) {
	s := newTestStore(t, "A")
	_, _, _ = s.Write("A", "k", "1")
	if _, _, err := s.Merge("A", "k", "x", []string{"A-99"}); err == nil {
		t.Fatal("expected error merging an unknown context version")
	}
}

func TestStore_PartialMergeContextKeepsKnownSibling(t *testing.T) {
	// A holds THREE concurrent siblings (writes by A, B and C that were
	// synced in). The client merges only A's and B's versions, deliberately
	// leaving C out of the context. The merge version must dominate A and B
	// but be CONCURRENT with C, so C must survive — an explicit merge is not
	// allowed to erase a version the client did not declare as merged.
	s := newTestStore(t, "A", "B", "C")
	k := "cfg"
	vA, _, _ := s.Write("A", k, "a")
	vB, _, _ := s.Write("B", k, "b")
	vC, _, _ := s.Write("C", k, "c")
	if _, err := s.Sync("A", "B", SyncModeTwoWay); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Sync("A", "C", SyncModeTwoWay); err != nil {
		t.Fatal(err)
	}
	if got := readIDs(t, s, "A", k); !reflect.DeepEqual(got, []string{vA.ID, vB.ID, vC.ID}) {
		t.Fatalf("setup: A siblings = %v", got)
	}

	merged, survivors, err := s.Merge("A", k, "a+b", []string{vA.ID, vB.ID})
	if err != nil {
		t.Fatal(err)
	}
	if CompareClocks(merged.Clock, vC.Clock) != ClockConcurrent {
		t.Fatalf("merge clock %v should be concurrent with omitted sibling %v",
			merged.Clock, vC.Clock)
	}
	want := []string{vC.ID, merged.ID}
	sort.Strings(want)
	if got := ids(survivors); !reflect.DeepEqual(got, want) {
		t.Fatalf("partial-merge survivors = %v, want %v (omitted concurrent sibling erased)", got, want)
	}

	// Local event counter still advances monotonically: a later plain write
	// gets the next id and dominates everything currently stored.
	v2, survivors2, err := s.Write("A", k, "overwrite-all")
	if err != nil {
		t.Fatal(err)
	}
	if v2.ID != "A-3" {
		t.Fatalf("event counter regressed: next write id = %q, want A-3", v2.ID)
	}
	if got := ids(survivors2); !reflect.DeepEqual(got, []string{v2.ID}) {
		t.Fatalf("plain overwrite after partial merge = %v, want only %s", got, v2.ID)
	}
}

func TestStore_SyncOneWayIsDirectional(t *testing.T) {
	s := newTestStore(t, "A", "B")
	vA, _, _ := s.Write("A", "k", "a")
	vB, _, _ := s.Write("B", "k", "b")

	report, err := s.Sync("A", "B", SyncModeOneWay)
	if err != nil {
		t.Fatal(err)
	}
	if report.Outbound[0].Accepted != 1 {
		t.Fatalf("outbound = %+v", report.Outbound[0])
	}
	// B learns A's write; A must NOT learn B's.
	if got := readIDs(t, s, "B", "k"); !reflect.DeepEqual(got, []string{vA.ID, vB.ID}) {
		t.Fatalf("B = %v", got)
	}
	if got := readIDs(t, s, "A", "k"); !reflect.DeepEqual(got, []string{vA.ID}) {
		t.Fatalf("A should stay isolated, got %v", got)
	}
}
