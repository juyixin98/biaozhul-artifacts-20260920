package vclock

import (
	"testing"
)

func TestCompare(t *testing.T) {
	cases := []struct {
		name string
		a, b Clock
		want Relation
	}{
		{"empty equal", Clock{}, Clock{}, Equal},
		{"nil equal", nil, Clock{}, Equal},
		{"simple before", Clock{"a": 1}, Clock{"a": 2}, Before},
		{"simple after", Clock{"a": 2}, Clock{"a": 1}, After},
		{"merge history before",
			Clock{"a": 1, "b": 1},
			Clock{"a": 2, "b": 1}, Before},
		{"missing component counts as zero",
			Clock{"a": 1},
			Clock{"a": 1, "b": 1}, Before},
		{"concurrent independent events",
			Clock{"a": 1},
			Clock{"b": 1}, Concurrent},
		{"concurrent after divergence",
			Clock{"a": 2, "b": 1},
			Clock{"a": 1, "b": 2}, Concurrent},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := Compare(tc.a, tc.b); got != tc.want {
				t.Fatalf("Compare = %s, want %s", got, tc.want)
			}
			// Symmetry: swapping a,b flips Before/After and keeps others.
			rev := Compare(tc.b, tc.a)
			switch tc.want {
			case Before:
				if rev != After {
					t.Fatalf("reverse Compare = %s, want after", rev)
				}
			case After:
				if rev != Before {
					t.Fatalf("reverse Compare = %s, want before", rev)
				}
			default:
				if rev != tc.want {
					t.Fatalf("reverse Compare = %s, want %s", rev, tc.want)
				}
			}
		})
	}
}

func TestJoinAndIncrement(t *testing.T) {
	a := Clock{"a": 2, "b": 1}
	b := Clock{"a": 1, "b": 3, "c": 1}
	got := Join(a, b)
	want := Clock{"a": 2, "b": 3, "c": 1}
	if Compare(got, want) != Equal {
		t.Fatalf("Join = %v, want %v", got, want)
	}
	// Join must not mutate inputs.
	if a["c"] != 0 || a["b"] != 1 {
		t.Fatalf("Join mutated input: %v", a)
	}

	inc := Increment(Clock{"a": 1}, "a")
	if inc["a"] != 2 {
		t.Fatalf("Increment = %v, want a=2", inc)
	}
}

func TestCanonicalStable(t *testing.T) {
	c1 := Clock{"b": 2, "a": 1, "c": 0}
	c2 := Clock{"a": 1, "b": 2}
	if c1.Canonical() != c2.Canonical() {
		t.Fatalf("equivalent clocks rendered differently: %q vs %q",
			c1.Canonical(), c2.Canonical())
	}
	if c1.Canonical() != "a=1,b=2," {
		t.Fatalf("unexpected canonical form: %q", c1.Canonical())
	}
}
