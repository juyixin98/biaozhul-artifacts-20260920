package clock

import "testing"

func TestCompare(t *testing.T) {
	vc := func(pairs ...any) VectorClock {
		out := VectorClock{}
		for i := 0; i < len(pairs); i += 2 {
			out[pairs[i].(string)] = pairs[i+1].(int)
		}
		return out
	}

	cases := []struct {
		name       string
		a, b       VectorClock
		want       Relation
		descendant bool // Descends(b, a)
	}{
		{"both empty", vc(), vc(), RelEqual, true},
		{"a before b (single key)", vc("A", 1), vc("A", 2), RelBefore, true},
		{"a after b (single key)", vc("A", 3), vc("A", 2), RelAfter, false},
		{"divergent -> concurrent", vc("A", 1, "B", 0), vc("A", 0, "B", 1), RelConcurrent, false},
		{"extra knowledge does not make concurrent", vc("A", 1, "B", 2), vc("A", 1, "B", 1, "C", 1), RelConcurrent, false},
		{"a prefixes b", vc("A", 1), vc("A", 1, "B", 1), RelBefore, true},
		{"equal multi-key", vc("A", 1, "B", 2), vc("A", 1, "B", 2), RelEqual, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := Compare(tc.a, tc.b); got != tc.want {
				t.Fatalf("Compare = %v, want %v", got, tc.want)
			}
			if got := Descends(tc.b, tc.a); got != tc.descendant {
				t.Fatalf("Descends(b, a) = %v, want %v", got, tc.descendant)
			}
		})
	}
}

func TestTickAndMerge(t *testing.T) {
	a := VectorClock{"A": 1}
	b := Tick(a, "A")
	if a["A"] != 1 {
		t.Fatalf("Tick mutated original clock: %v", a)
	}
	if b["A"] != 2 {
		t.Fatalf("Tick did not increment: %v", b)
	}

	merged := Merge(VectorClock{"A": 2, "B": 1}, VectorClock{"A": 1, "B": 3, "C": 1})
	want := VectorClock{"A": 2, "B": 3, "C": 1}
	if !Equal(merged, want) {
		t.Fatalf("Merge = %v, want %v", merged, want)
	}
}
