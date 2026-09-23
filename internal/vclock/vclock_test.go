package vclock

import "testing"

func TestCompare(t *testing.T) {
	cases := []struct {
		name string
		a, b Clock
		want Relation
	}{
		{"both empty", Clock{}, Clock{}, EqualRel},
		{"a empty", Clock{}, Clock{"n1": 1}, Before},
		{"b empty", Clock{"n1": 1}, Clock{}, After},
		{"strict ancestor", Clock{"n1": 1}, Clock{"n1": 1, "n2": 1}, Before},
		{"strict descendant", Clock{"n1": 2, "n2": 1}, Clock{"n1": 1, "n2": 1}, After},
		{"concurrent", Clock{"n1": 1, "n2": 0}, Clock{"n1": 0, "n2": 1}, Concurrent},
		{"diverged", Clock{"n1": 2, "n2": 1}, Clock{"n1": 1, "n2": 2}, Concurrent},
		{"equal nonzero", Clock{"n1": 2, "n2": 3}, Clock{"n1": 2, "n2": 3}, EqualRel},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := Compare(tc.a, tc.b); got != tc.want {
				t.Fatalf("Compare(%v,%v)=%s want %s", tc.a, tc.b, got, tc.want)
			}
			// Inverse consistency.
			inv := map[Relation]Relation{Before: After, After: Before, EqualRel: EqualRel, Concurrent: Concurrent}
			if got := Compare(tc.b, tc.a); got != inv[tc.want] {
				t.Fatalf("inverse Compare=%s want %s", got, inv[tc.want])
			}
		})
	}
}

func TestMerge(t *testing.T) {
	got := Merged(Clock{"n1": 1, "n2": 5}, Clock{"n1": 3, "n3": 2})
	want := Clock{"n1": 3, "n2": 5, "n3": 2}
	if Compare(got, want) != EqualRel {
		t.Fatalf("Merged=%v want %v", got, want)
	}
	// Inputs must not be mutated.
	if (Clock{"n1": 1, "n2": 5})["n1"] != 1 {
		t.Fatal("Merged mutated its input")
	}
}
