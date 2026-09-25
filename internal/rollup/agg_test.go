package rollup

import (
	"math"
	"testing"
)

func TestAlignStart(t *testing.T) {
	cases := []struct {
		name string
		t    int64
		w    int64
		want int64
	}{
		{"exact boundary", 120, 60, 120},
		{"inside minute", 125, 60, 120},
		{"last second", 179, 60, 120},
		{"hour boundary", 3600, 3600, 3600},
		{"inside hour", 3661, 3600, 3600},
		{"zero", 0, 60, 0},
		{"raw width", 42, 1, 42},
		{"negative floors down", -1, 60, -60},
		{"negative boundary", -60, 60, -60},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := AlignStart(tc.t, tc.w); got != tc.want {
				t.Errorf("AlignStart(%d, %d) = %d, want %d", tc.t, tc.w, got, tc.want)
			}
		})
	}
}

func TestMerge(t *testing.T) {
	cases := []struct {
		name string
		a, b Agg
		want Agg
	}{
		{"both empty", Agg{}, Agg{}, Agg{}},
		{"a empty", Agg{}, Agg{Count: 2, Sum: 5, Min: 1, Max: 4}, Agg{Count: 2, Sum: 5, Min: 1, Max: 4}},
		{"b empty", Agg{Count: 2, Sum: 5, Min: 1, Max: 4}, Agg{}, Agg{Count: 2, Sum: 5, Min: 1, Max: 4}},
		{
			"disjoint samples",
			Agg{Count: 2, Sum: 4, Min: 1, Max: 3},
			Agg{Count: 3, Sum: 30, Min: 5, Max: 15},
			Agg{Count: 5, Sum: 34, Min: 1, Max: 15},
		},
		{
			"nested extremes",
			Agg{Count: 10, Sum: 100, Min: -5, Max: 20},
			Agg{Count: 1, Sum: 0, Min: 0, Max: 0},
			Agg{Count: 11, Sum: 100, Min: -5, Max: 20},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := Merge(tc.a, tc.b)
			if got != tc.want {
				t.Errorf("Merge = %+v, want %+v", got, tc.want)
			}
		})
	}
}

func TestMergeIsCommutative(t *testing.T) {
	// Late arrival handling relies on order-independent merges.
	a := Agg{Count: 3, Sum: 12, Min: -2, Max: 9}
	b := Agg{Count: 7, Sum: 40, Min: 1, Max: 6}
	if Merge(a, b) != Merge(b, a) {
		t.Fatal("merge must be commutative for out-of-order ingest")
	}
}

func TestMeanAndEmpty(t *testing.T) {
	if !(Agg{}).Empty() {
		t.Error("zero-count aggregate must be empty")
	}
	if got := (Agg{}).Mean(); got != 0 {
		t.Errorf("mean of empty = %v, want 0", got)
	}
	a := Agg{Count: 4, Sum: 10}
	if a.Empty() {
		t.Error("non-zero count must not be empty")
	}
	if got, want := a.Mean(), 2.5; got != want {
		t.Errorf("mean = %v, want %v", got, want)
	}
}

// TestMergeMatchesRawMultiset is the core "never re-average means" check:
// merging bucket summaries must equal aggregating the raw multiset.
func TestMergeMatchesRawMultiset(t *testing.T) {
	raw := []float64{1, 2, 2, 3, 100, -4, 0.5}
	// Split the raw values into two pseudo-buckets and merge summaries.
	left := Agg{}
	right := Agg{}
	for i, v := range raw {
		if i%2 == 0 {
			left = Merge(left, FromSample(v))
		} else {
			right = Merge(right, FromSample(v))
		}
	}
	got := Merge(left, right)

	want := Agg{}
	for _, v := range raw {
		want = Merge(want, FromSample(v))
	}
	if got.Count != want.Count || got.Sum != want.Sum || got.Min != want.Min || got.Max != want.Max {
		t.Fatalf("merged summaries %+v != raw multiset %+v", got, want)
	}
	if math.Abs(got.Mean()-14.928571428571429) > 1e-12 {
		t.Errorf("mean = %v", got.Mean())
	}
}
