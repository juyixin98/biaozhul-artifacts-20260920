package main

import (
	"reflect"
	"testing"
)

func TestCompareClocks(t *testing.T) {
	cases := []struct {
		name string
		a, b Clock
		want ClockOrder
	}{
		{"two empty", Clock{}, Clock{}, ClockEqual},
		{"nil treated as empty", nil, Clock{}, ClockEqual},
		{"causal", Clock{"A": 1}, Clock{"A": 2}, ClockBefore},
		{"reverse causal", Clock{"A": 2}, Clock{"A": 1}, ClockAfter},
		{"extra knowledge dominates", Clock{"A": 1}, Clock{"A": 1, "B": 1}, ClockBefore},
		{"concurrent disjoint", Clock{"A": 1}, Clock{"B": 1}, ClockConcurrent},
		{
			"concurrent with shared history",
			Clock{"A": 2, "B": 1},
			Clock{"A": 1, "B": 2},
			ClockConcurrent,
		},
		{
			"equal multi-entry",
			Clock{"A": 2, "B": 1},
			Clock{"B": 1, "A": 2},
			ClockEqual,
		},
		{
			"strictly ahead on every component",
			Clock{"A": 1, "B": 1},
			Clock{"A": 2, "B": 2},
			ClockBefore,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := CompareClocks(tc.a, tc.b); got != tc.want {
				t.Fatalf("CompareClocks(%v, %v) = %v, want %v", tc.a, tc.b, got, tc.want)
			}
		})
	}
}

func TestClockJoin(t *testing.T) {
	got := ClockJoin(Clock{"A": 1, "B": 1}, Clock{"A": 2, "C": 1}, nil)
	want := Clock{"A": 2, "B": 1, "C": 1}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("join = %v, want %v", got, want)
	}
	// Join must not mutate its inputs.
	in := Clock{"A": 1}
	ClockJoin(in, Clock{"A": 5})
	if in["A"] != 1 {
		t.Fatalf("join mutated input clock: %v", in)
	}
}

func ver(id string, clock Clock, value string) Version {
	origin := string([]rune(id)[0])
	return Version{ID: id, Origin: origin, Clock: Clock(clock), Value: value}
}

func ids(vs []Version) []string {
	out := make([]string, len(vs))
	for i, v := range vs {
		out[i] = v.ID
	}
	return out
}

func TestMergeVersions_CausalOverwrite(t *testing.T) {
	old := ver("A-1", Clock{"A": 1}, "old")
	next := ver("A-2", Clock{"A": 2}, "new")

	merged, outcomes := MergeVersions([]Version{old}, []Version{next})
	if got := ids(merged); !reflect.DeepEqual(got, []string{"A-2"}) {
		t.Fatalf("survivors = %v, want [A-2]", got)
	}
	if outcomes["A-2"] != OutcomeAccepted {
		t.Fatalf("outcome = %v, want accepted", outcomes["A-2"])
	}

	// A delayed old write arriving afterwards must be dropped, never kept.
	merged, outcomes = MergeVersions(merged, []Version{old})
	if got := ids(merged); !reflect.DeepEqual(got, []string{"A-2"}) {
		t.Fatalf("late old write kept: survivors = %v", got)
	}
	if outcomes["A-1"] != OutcomeSuperseded {
		t.Fatalf("late old write outcome = %v, want superseded", outcomes["A-1"])
	}
}

func TestMergeVersions_ConcurrentSiblingsKept(t *testing.T) {
	vA := ver("A-1", Clock{"A": 1}, "from A")
	vB := ver("B-1", Clock{"B": 1}, "from B")

	merged, _ := MergeVersions(nil, []Version{vA, vB})
	if got := ids(merged); !reflect.DeepEqual(got, []string{"A-1", "B-1"}) {
		t.Fatalf("siblings = %v, want both", got)
	}

	// Merging the same antichain again (a no-op sync) changes nothing.
	again, _ := MergeVersions(merged, merged)
	if got := ids(again); !reflect.DeepEqual(got, []string{"A-1", "B-1"}) {
		t.Fatalf("re-merge = %v, want stable antichain", got)
	}
}

func TestMergeVersions_DuplicateIdempotent(t *testing.T) {
	vA := ver("A-1", Clock{"A": 1}, "x")
	merged, first := MergeVersions(nil, []Version{vA})
	if first["A-1"] != OutcomeAccepted {
		t.Fatalf("first delivery = %v, want accepted", first["A-1"])
	}
	// Re-delivery with identical id even at-most-once retry.
	merged, second := MergeVersions(merged, []Version{vA, vA})
	if second["A-1"] != OutcomeDuplicate {
		t.Fatalf("re-delivery = %v, want duplicate", second["A-1"])
	}
	if len(merged) != 1 {
		t.Fatalf("duplicated version stored: %v", ids(merged))
	}
}

func TestMergeVersions_ConvergenceAcrossOrders(t *testing.T) {
	// Scenario: A writes a1; B syncs a1 then writes b1 (so b1 causally
	// replaces a1); A writes a2 without ever seeing b1, which makes a2
	// concurrent with b1. Whichever order the three messages arrive in, the
	// final antichain must be exactly {A-2, B-1}: a1 is dominated, and the
	// concurrent version must not be deleted.
	a1 := ver("A-1", Clock{"A": 1}, "a1")
	b1 := ver("B-1", Clock{"A": 1, "B": 1}, "b1")
	a2 := ver("A-2", Clock{"A": 2}, "a2")
	all := []Version{a1, b1, a2}

	orders := [][]int{
		{0, 1, 2},
		{2, 1, 0},
		{1, 0, 2},
		{0, 2, 1},
		{1, 2, 0},
		{2, 0, 1},
	}
	var canonical []string
	for i, order := range orders {
		var got []Version
		for _, idx := range order {
			got, _ = MergeVersions(got, []Version{all[idx]})
		}
		gotIDs := ids(got)
		if i == 0 {
			canonical = gotIDs
		}
		if !reflect.DeepEqual(gotIDs, canonical) {
			t.Fatalf("order %v gives %v, want %v (convergence broken)", order, gotIDs, canonical)
		}
	}
	want := []string{"A-2", "B-1"}
	if !reflect.DeepEqual(canonical, want) {
		t.Fatalf("antichain = %v, want %v (concurrent version wrongly deleted)", canonical, want)
	}
}

func TestMergeVersions_MergedVersionDominatesContext(t *testing.T) {
	// After an explicit merge m with clock join(A,B)+1, both siblings are
	// dominated; a third sibling C concurrent with m survives.
	a := ver("A-2", Clock{"A": 2, "B": 1}, "a")
	b := ver("B-1", Clock{"A": 1, "B": 1}, "b")
	m := ver("C-1", Clock{"A": 2, "B": 1, "C": 1}, "merged")
	c := ver("D-1", Clock{"D": 1}, "late concurrent outsider")

	merged, _ := MergeVersions([]Version{a, b}, []Version{m})
	if got := ids(merged); !reflect.DeepEqual(got, []string{"C-1"}) {
		t.Fatalf("after merge survivors = %v, want [C-1]", got)
	}
	merged, _ = MergeVersions(merged, []Version{c})
	if got := ids(merged); !reflect.DeepEqual(got, []string{"C-1", "D-1"}) {
		t.Fatalf("concurrent outsider dropped: %v", got)
	}
}
