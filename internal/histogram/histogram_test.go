package histogram

import (
	"errors"
	"math"
	"testing"
)

func mustValid(t *testing.T, h *Histogram) {
	t.Helper()
	if err := h.Validate(); err != nil {
		t.Fatalf("expected valid histogram, got %v", err)
	}
}

func TestValidateOK(t *testing.T) {
	mustValid(t, &Histogram{Name: "ok", Bounds: []float64{1, 2, math.Inf(1)}, Counts: []uint64{3, 5, 9}})
	// single +Inf bucket
	mustValid(t, &Histogram{Name: "inf-only", Bounds: []float64{math.Inf(1)}, Counts: []uint64{7}})
	// empty histogram (all zero counts) is structurally valid
	mustValid(t, &Histogram{Name: "empty", Bounds: []float64{1, math.Inf(1)}, Counts: []uint64{0, 0}})
}

func TestValidateErrors(t *testing.T) {
	cases := []struct {
		name string
		h    *Histogram
		want error
	}{
		{"no buckets", &Histogram{Name: "x"}, ErrNoBuckets},
		{"length mismatch", &Histogram{Bounds: []float64{1, math.Inf(1)}, Counts: []uint64{1}}, ErrCountLen},
		{"unsorted bounds", &Histogram{Bounds: []float64{2, 1, math.Inf(1)}, Counts: []uint64{1, 2, 3}}, ErrBoundsNotSorted},
		{"duplicate bounds", &Histogram{Bounds: []float64{1, 1, math.Inf(1)}, Counts: []uint64{1, 2, 3}}, ErrBoundsNotSorted},
		{"missing +Inf", &Histogram{Bounds: []float64{1, 2}, Counts: []uint64{1, 2}}, ErrMissingInf},
		{"-Inf bound", &Histogram{Bounds: []float64{math.Inf(-1), math.Inf(1)}, Counts: []uint64{1, 2}}, ErrBoundsNotSorted},
		{"NaN bound", &Histogram{Bounds: []float64{math.NaN(), math.Inf(1)}, Counts: []uint64{1, 2}}, ErrBoundsNotSorted},
		// erroneous cumulative value: counts go down
		{"non-monotonic counts", &Histogram{Bounds: []float64{1, 2, math.Inf(1)}, Counts: []uint64{5, 3, 9}}, ErrCountsNotMono},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if err := c.h.Validate(); !errors.Is(err, c.want) {
				t.Fatalf("got %v, want %v", err, c.want)
			}
		})
	}
}

func TestMergeIdenticalBounds(t *testing.T) {
	a := &Histogram{Name: "a", Bounds: []float64{0.1, 0.5, 1, math.Inf(1)}, Counts: []uint64{1, 4, 9, 12}}
	b := &Histogram{Name: "b", Bounds: []float64{0.1, 0.5, 1, math.Inf(1)}, Counts: []uint64{2, 3, 8, 10}}
	m, err := Merge(a, b)
	if err != nil {
		t.Fatal(err)
	}
	want := []uint64{3, 7, 17, 22}
	for i := range want {
		if m.Counts[i] != want[i] {
			t.Fatalf("counts[%d]=%d, want %d", i, m.Counts[i], want[i])
		}
	}
	if m.Total() != a.Total()+b.Total() {
		t.Fatalf("count conservation violated: %d != %d", m.Total(), a.Total()+b.Total())
	}
}

func TestMergeShrinksToCommonCoarserBounds(t *testing.T) {
	// a is finer than b; common bounds are {0.5, 1, +Inf}.
	a := &Histogram{Name: "fine", Bounds: []float64{0.1, 0.5, 0.75, 1, math.Inf(1)}, Counts: []uint64{1, 4, 6, 9, 12}}
	b := &Histogram{Name: "coarse", Bounds: []float64{0.5, 1, math.Inf(1)}, Counts: []uint64{2, 8, 10}}
	m, err := Merge(a, b)
	if err != nil {
		t.Fatal(err)
	}
	wantBounds := []float64{0.5, 1, math.Inf(1)}
	if !sameBounds(m.Bounds, wantBounds) {
		t.Fatalf("bounds=%v, want %v", m.Bounds, wantBounds)
	}
	// a rebinned: {4, 9, 12}; b: {2, 8, 10}
	want := []uint64{6, 17, 22}
	for i := range want {
		if m.Counts[i] != want[i] {
			t.Fatalf("counts[%d]=%d, want %d", i, m.Counts[i], want[i])
		}
	}
	if m.Total() != 22 {
		t.Fatalf("total=%d, want 22", m.Total())
	}
}

func TestMergeDisjointBoundsCollapseToInf(t *testing.T) {
	// No shared finite bounds: only +Inf is common, so everything collapses
	// into a single bucket. Totals are still conserved.
	a := &Histogram{Name: "a", Bounds: []float64{1, math.Inf(1)}, Counts: []uint64{3, 5}}
	b := &Histogram{Name: "b", Bounds: []float64{2, math.Inf(1)}, Counts: []uint64{4, 7}}
	m, err := Merge(a, b)
	if err != nil {
		t.Fatal(err)
	}
	if len(m.Bounds) != 1 || !math.IsInf(m.Bounds[0], 1) {
		t.Fatalf("bounds=%v, want [+Inf]", m.Bounds)
	}
	if m.Total() != 12 {
		t.Fatalf("total=%d, want 12", m.Total())
	}
}

func TestMergeEmptyHistogram(t *testing.T) {
	full := &Histogram{Name: "full", Bounds: []float64{1, math.Inf(1)}, Counts: []uint64{3, 5}}
	empty := &Histogram{Name: "empty", Bounds: []float64{1, math.Inf(1)}, Counts: []uint64{0, 0}}
	m, err := Merge(full, empty)
	if err != nil {
		t.Fatal(err)
	}
	if m.Total() != 5 {
		t.Fatalf("total=%d, want 5", m.Total())
	}
	// empty + empty stays empty
	m2, err := Merge(empty, &Histogram{Name: "empty2", Bounds: []float64{1, math.Inf(1)}, Counts: []uint64{0, 0}})
	if err != nil {
		t.Fatal(err)
	}
	if m2.Total() != 0 {
		t.Fatalf("total=%d, want 0", m2.Total())
	}
}

func TestMergeRejectsInvalidInput(t *testing.T) {
	good := &Histogram{Name: "good", Bounds: []float64{1, math.Inf(1)}, Counts: []uint64{3, 5}}
	bad := &Histogram{Name: "bad", Bounds: []float64{1, math.Inf(1)}, Counts: []uint64{9, 5}} // decreasing
	if _, err := Merge(good, bad); !errors.Is(err, ErrCountsNotMono) {
		t.Fatalf("got %v, want ErrCountsNotMono", err)
	}
	if _, err := Merge(bad, good); !errors.Is(err, ErrCountsNotMono) {
		t.Fatalf("got %v, want ErrCountsNotMono", err)
	}
}

func TestMergeAllConservesTotal(t *testing.T) {
	hs := []*Histogram{
		{Name: "h1", Bounds: []float64{0.5, 1, 2, math.Inf(1)}, Counts: []uint64{1, 3, 6, 10}},
		{Name: "h2", Bounds: []float64{1, 2, math.Inf(1)}, Counts: []uint64{2, 5, 8}},
		{Name: "h3", Bounds: []float64{2, math.Inf(1)}, Counts: []uint64{4, 4}},
	}
	var sum uint64
	for _, h := range hs {
		sum += h.Total()
	}
	m, err := MergeAll(hs)
	if err != nil {
		t.Fatal(err)
	}
	if m.Total() != sum {
		t.Fatalf("total=%d, want %d", m.Total(), sum)
	}
	// common bounds of all three: {2, +Inf}
	if !sameBounds(m.Bounds, []float64{2, math.Inf(1)}) {
		t.Fatalf("bounds=%v, want [2 +Inf]", m.Bounds)
	}
}

func TestQuantileIntervalAndEstimate(t *testing.T) {
	h := &Histogram{Name: "q", Bounds: []float64{1, 2, 4, math.Inf(1)}, Counts: []uint64{10, 30, 60, 100}}

	q50, err := h.Quantile(0.5)
	if err != nil {
		t.Fatal(err)
	}
	// rank=50 falls in bucket (2,4]: cumulative 30..60
	if q50.Lower != 2 || q50.Upper != 4 {
		t.Fatalf("interval=[%v,%v], want [2,4]", q50.Lower, q50.Upper)
	}
	// interpolation: 2 + (50-30)/(60-30) * (4-2) = 2 + 2/3*2
	want := 2 + (50.0-30.0)/(60.0-30.0)*2
	if math.Abs(float64(q50.Estimate)-want) > 1e-9 {
		t.Fatalf("estimate=%v, want %v", q50.Estimate, want)
	}
	if q50.Estimate < q50.Lower || q50.Estimate > q50.Upper {
		t.Fatalf("estimate %v outside interval [%v,%v]", q50.Estimate, q50.Lower, q50.Upper)
	}
}

func TestQuantileInfinityBucket(t *testing.T) {
	h := &Histogram{Name: "q", Bounds: []float64{1, math.Inf(1)}, Counts: []uint64{8, 10}}
	q, err := h.Quantile(0.95)
	if err != nil {
		t.Fatal(err)
	}
	// rank=9.5 lands in the +Inf bucket: interval (1, +Inf], estimate pinned to lower bound
	if q.Lower != 1 || !math.IsInf(float64(q.Upper), 1) {
		t.Fatalf("interval=[%v,%v], want [1,+Inf]", q.Lower, q.Upper)
	}
	if q.Estimate != 1 {
		t.Fatalf("estimate=%v, want 1 (lower bound of open bucket)", q.Estimate)
	}
}

func TestQuantileEmptyHistogram(t *testing.T) {
	h := &Histogram{Name: "empty", Bounds: []float64{1, math.Inf(1)}, Counts: []uint64{0, 0}}
	if _, err := h.Quantile(0.5); !errors.Is(err, ErrEmptyHistogram) {
		t.Fatalf("got %v, want ErrEmptyHistogram", err)
	}
}

func TestQuantileBadQ(t *testing.T) {
	h := &Histogram{Name: "q", Bounds: []float64{math.Inf(1)}, Counts: []uint64{1}}
	for _, q := range []float64{-0.1, 1.1, math.NaN()} {
		if _, err := h.Quantile(q); !errors.Is(err, ErrBadQuantile) {
			t.Fatalf("q=%v: got %v, want ErrBadQuantile", q, err)
		}
	}
}

func TestRebinLossless(t *testing.T) {
	h := &Histogram{Name: "r", Bounds: []float64{0.1, 0.5, 1, 5, math.Inf(1)}, Counts: []uint64{2, 7, 9, 11, 15}}
	r, err := h.Rebin([]float64{0.5, 5, math.Inf(1)})
	if err != nil {
		t.Fatal(err)
	}
	want := []uint64{7, 11, 15}
	for i := range want {
		if r.Counts[i] != want[i] {
			t.Fatalf("counts[%d]=%d, want %d", i, r.Counts[i], want[i])
		}
	}
	if r.Total() != h.Total() {
		t.Fatal("rebin must conserve total")
	}
	if _, err := h.Rebin([]float64{0.2, math.Inf(1)}); err == nil {
		t.Fatal("rebin to a non-subset bound must fail")
	}
}
