package histogram_test

import (
	"encoding/json"
	"errors"
	"math"
	"testing"

	"histmerge/internal/histogram"
)

// helper builders ------------------------------------------------------------

func fb(v float64) histogram.Bound { return histogram.FiniteBound(v) }

// cum builds a cumulative histogram from per-bucket incremental counts;
// the last element is the +Inf bucket.
func cum(sum float64, upper []float64, counts ...uint64) *histogram.Histogram {
	if len(counts) != len(upper)+1 {
		panic("counts must be len(upper)+1")
	}
	h := &histogram.Histogram{Sum: sum}
	var run uint64
	for i, c := range counts {
		run += c
		var ub histogram.Bound
		if i < len(upper) {
			ub = fb(upper[i])
		} else {
			ub = histogram.InfBound()
		}
		h.Buckets = append(h.Buckets, histogram.Bucket{Upper: ub, CumulativeCount: run})
	}
	h.TotalCount = run
	return h
}

func mustValidate(t *testing.T, h *histogram.Histogram) {
	t.Helper()
	if err := h.Validate(); err != nil {
		t.Fatalf("unexpected validation error: %v", err)
	}
}

// validation -----------------------------------------------------------------

func TestValidate_OK(t *testing.T) {
	h := cum(10, []float64{1, 2, 4}, 1, 2, 3, 4)
	mustValidate(t, h)
}

func TestValidate_MissingInfBucket(t *testing.T) {
	h := &histogram.Histogram{
		TotalCount: 3,
		Sum:        6,
		Buckets: []histogram.Bucket{
			{Upper: fb(1), CumulativeCount: 1},
			{Upper: fb(2), CumulativeCount: 3},
		},
	}
	err := h.Validate()
	if !errors.Is(err, histogram.ErrInvalidHistogram) {
		t.Fatalf("want ErrInvalidHistogram, got %v", err)
	}
}

func TestValidate_MonotonicDecrease(t *testing.T) {
	h := &histogram.Histogram{
		TotalCount: 5,
		Sum:        10,
		Buckets: []histogram.Bucket{
			{Upper: fb(1), CumulativeCount: 3},
			{Upper: fb(2), CumulativeCount: 2}, // decrease
			{Upper: histogram.InfBound(), CumulativeCount: 5},
		},
	}
	if err := h.Validate(); err == nil || !errors.Is(err, histogram.ErrInvalidHistogram) {
		t.Fatalf("want invalid (monotonic decrease), got %v", err)
	}
}

func TestValidate_InfCountMismatch(t *testing.T) {
	h := &histogram.Histogram{
		TotalCount: 9,
		Sum:        10,
		Buckets: []histogram.Bucket{
			{Upper: fb(1), CumulativeCount: 3},
			{Upper: histogram.InfBound(), CumulativeCount: 8}, // != total
		},
	}
	if err := h.Validate(); !errors.Is(err, histogram.ErrInvalidHistogram) {
		t.Fatalf("want invalid (+Inf mismatch), got %v", err)
	}
}

func TestValidate_EmptyHistogramMustBeZeroCounts(t *testing.T) {
	bad := &histogram.Histogram{
		TotalCount: 0,
		Buckets: []histogram.Bucket{
			{Upper: fb(1), CumulativeCount: 1}, // not zero
			{Upper: histogram.InfBound(), CumulativeCount: 0},
		},
	}
	if err := bad.Validate(); !errors.Is(err, histogram.ErrInvalidHistogram) {
		t.Fatalf("empty histogram with stray counts must be invalid, got %v", err)
	}
	good := &histogram.Histogram{Buckets: []histogram.Bucket{
		{Upper: fb(1)}, {Upper: histogram.InfBound()},
	}}
	mustValidate(t, good)
}

func TestValidate_DuplicateBounds(t *testing.T) {
	h := &histogram.Histogram{
		TotalCount: 1, Sum: 1,
		Buckets: []histogram.Bucket{
			{Upper: fb(1), CumulativeCount: 1},
			{Upper: fb(1), CumulativeCount: 1},
			{Upper: histogram.InfBound(), CumulativeCount: 1},
		},
	}
	if err := h.Validate(); !errors.Is(err, histogram.ErrInvalidHistogram) {
		t.Fatalf("duplicate bounds must be invalid, got %v", err)
	}
}

// Bound JSON ----------------------------------------------------------------

func TestBound_JSONRoundtrip(t *testing.T) {
	bs := []histogram.Bound{fb(0.25), fb(10), histogram.InfBound()}
	data, err := json.Marshal(bs)
	if err != nil {
		t.Fatal(err)
	}
	want := `[0.25,10,"+Inf"]`
	if string(data) != want {
		t.Fatalf("marshal = %s, want %s", data, want)
	}
	var back []histogram.Bound
	if err := json.Unmarshal(data, &back); err != nil {
		t.Fatal(err)
	}
	for i := range bs {
		if bs[i].Cmp(back[i]) != 0 {
			t.Fatalf("bound %d mismatch: %v vs %v", i, bs[i], back[i])
		}
	}
}

// direct merge ---------------------------------------------------------------

func TestMergeStrict_CountConservation(t *testing.T) {
	a := cum(10, []float64{1, 2}, 1, 2, 3) // total 6
	b := cum(20, []float64{1, 2}, 0, 4, 2) // total 6
	mustValidate(t, a)
	mustValidate(t, b)

	res, err := histogram.MergeStrict(a, b)
	if err != nil {
		t.Fatal(err)
	}
	m := res.Merged
	// bucket incrementals: (1+0),(2+4),(3+2) => cumulative 1,7,12
	wantCounts := []uint64{1, 7, 12}
	for i, w := range wantCounts {
		if m.Buckets[i].CumulativeCount != w {
			t.Fatalf("bucket %d = %d, want %d", i, m.Buckets[i].CumulativeCount, w)
		}
	}
	if m.TotalCount != 12 {
		t.Fatalf("total = %d, want 12", m.TotalCount)
	}
	if !res.Conserved {
		t.Fatal("Conserved should be true: 6+6 == 12")
	}
	if res.InputTotalCounts != 12 {
		t.Fatalf("input totals = %d", res.InputTotalCounts)
	}
	if !res.BucketConserved {
		t.Fatal("bucket-level conservation should hold for direct merge")
	}
	if math.Abs(m.Sum-30) > 1e-9 {
		t.Fatalf("sum = %v, want 30", m.Sum)
	}
}

func TestMergeStrict_Incompatible(t *testing.T) {
	a := cum(1, []float64{1, 2}, 1, 0, 0)
	b := cum(1, []float64{1, 3}, 1, 0, 0)
	if _, err := histogram.MergeStrict(a, b); !errors.Is(err, histogram.ErrIncompatibleLayout) {
		t.Fatalf("want ErrIncompatibleLayout, got %v", err)
	}
}

// coarsening -----------------------------------------------------------------

func TestCommonBounds_Intersection(t *testing.T) {
	a := cum(1, []float64{1, 2, 4}, 1, 0, 0, 0)
	b := cum(1, []float64{2, 4, 8}, 1, 0, 0, 0)
	common, err := histogram.CommonBounds(a, b)
	if err != nil {
		t.Fatal(err)
	}
	want := []histogram.Bound{fb(2), fb(4), histogram.InfBound()}
	if len(common) != len(want) {
		t.Fatalf("common = %v, want %v", common, want)
	}
	for i := range want {
		if common[i].Cmp(want[i]) != 0 {
			t.Fatalf("common[%d] = %s, want %s", i, common[i], want[i])
		}
	}
}

func TestContract_OnlySubsetAllowed(t *testing.T) {
	a := cum(1, []float64{1, 2, 4}, 1, 0, 0, 0)
	if _, err := a.Contract([]histogram.Bound{fb(3), histogram.InfBound()}); !errors.Is(err, histogram.ErrNotAContract) {
		t.Fatalf("contract to non-present bound must fail, got %v", err)
	}
	c, err := a.Contract([]histogram.Bound{fb(2), histogram.InfBound()})
	if err != nil {
		t.Fatal(err)
	}
	if len(c.Buckets) != 2 {
		t.Fatalf("contracted buckets = %d, want 2", len(c.Buckets))
	}
}

func TestMergeCoarsen_CountConservation(t *testing.T) {
	// Different layouts; shared finite bounds {2,4}.
	a := cum(10, []float64{1, 2, 4}, 1, 2, 3, 4) // inc:1,2,3,4 total10
	b := cum(20, []float64{2, 4, 8}, 2, 3, 4, 5) // inc:2,3,4,5 total14
	mustValidate(t, a)
	mustValidate(t, b)

	res, err := histogram.MergeCoarsen(a, b)
	if err != nil {
		t.Fatal(err)
	}
	if !res.Coarsened {
		t.Fatal("expected coarsened merge")
	}
	m := res.Merged
	// At shared bound 2: a cum<=2 = 3, b cum<=2 = 2 -> 5
	// At shared bound 4: a cum<=4 = 6, b cum<=4 = 5 -> 11
	// +Inf: 10+14 = 24
	if m.Buckets[0].CumulativeCount != 5 || m.Buckets[1].CumulativeCount != 11 || m.Buckets[2].CumulativeCount != 24 {
		t.Fatalf("coarsened counts = %d,%d,%d want 5,11,24",
			m.Buckets[0].CumulativeCount, m.Buckets[1].CumulativeCount, m.Buckets[2].CumulativeCount)
	}
	if m.TotalCount != 24 || !res.Conserved || !res.BucketConserved {
		t.Fatalf("conservation broken: total=%d conserved=%v bucket=%v inputTotal=%d",
			m.TotalCount, res.Conserved, res.BucketConserved, res.InputTotalCounts)
	}
}

func TestMergeCoarsen_NoCommonFiniteBounds(t *testing.T) {
	a := cum(1, []float64{1}, 1, 0)
	b := cum(1, []float64{2}, 1, 0)
	if _, err := histogram.MergeCoarsen(a, b); !errors.Is(err, histogram.ErrNoCommonBounds) {
		t.Fatalf("want ErrNoCommonBounds, got %v", err)
	}
}

func TestMerge_AutoStrategy(t *testing.T) {
	a := cum(10, []float64{1, 2, 4}, 1, 2, 3, 4)
	b := cum(10, []float64{2, 4, 8}, 1, 2, 3, 4)
	res, err := histogram.Merge(a, b)
	if err != nil {
		t.Fatal(err)
	}
	if !res.Coarsened {
		t.Fatal("incompatible layouts must trigger coarsening automatically")
	}
	if !res.Conserved {
		t.Fatal("total count must be conserved after contraction")
	}
}

// empty histograms -----------------------------------------------------------

func TestMerge_EmptyIsIdentity(t *testing.T) {
	empty := &histogram.Histogram{Buckets: []histogram.Bucket{
		{Upper: fb(1)}, {Upper: fb(2)}, {Upper: histogram.InfBound()},
	}}
	a := cum(10, []float64{1, 2}, 1, 2, 3)
	mustValidate(t, empty)
	mustValidate(t, a)

	res, err := histogram.Merge(empty, a)
	if err != nil {
		t.Fatal(err)
	}
	if res.Merged.TotalCount != 6 {
		t.Fatalf("merge with empty = total %d, want 6", res.Merged.TotalCount)
	}
	if !res.Conserved {
		t.Fatal("conservation with empty input must hold")
	}
}

func TestMerge_AllEmpty(t *testing.T) {
	e1 := &histogram.Histogram{Buckets: []histogram.Bucket{{Upper: histogram.InfBound()}}}
	e2 := &histogram.Histogram{Buckets: []histogram.Bucket{{Upper: histogram.InfBound()}}}
	res, err := histogram.Merge(e1, e2)
	if err != nil {
		t.Fatal(err)
	}
	if res.Merged.TotalCount != 0 {
		t.Fatalf("all-empty merge total = %d, want 0", res.Merged.TotalCount)
	}
}

func TestQuantile_EmptyHistogram(t *testing.T) {
	e := &histogram.Histogram{Buckets: []histogram.Bucket{{Upper: histogram.InfBound()}}}
	if _, err := histogram.EstimateQuantile(e, 0.5); !errors.Is(err, histogram.ErrEmptyHistogram) {
		t.Fatalf("want ErrEmptyHistogram, got %v", err)
	}
}

// +Inf bucket ----------------------------------------------------------------

func TestQuantile_AllInInfBucket(t *testing.T) {
	// 3 observations, all above 10 -> only +Inf bucket has counts.
	h := cum(100, []float64{1, 10}, 0, 0, 3)
	mustValidate(t, h)
	e, err := histogram.EstimateQuantile(h, 0.5)
	if err != nil {
		t.Fatal(err)
	}
	if e.Point != nil {
		t.Fatalf("point should be unknown inside +Inf bucket, got %v", *e.Point)
	}
	// Every observation is > 10, so the honest interval is (10,+Inf).
	if e.Lower == nil || *e.Lower != 10 {
		t.Fatalf("lower edge should be 10, got %v", e.Lower)
	}
	if e.Upper != nil {
		t.Fatalf("upper edge should be +Inf(nil), got %v", *e.Upper)
	}
}

func TestQuantile_OnlyInfBucketLayout(t *testing.T) {
	// Layout with nothing but a +Inf bucket: interval is fully open.
	h := &histogram.Histogram{
		TotalCount: 2, Sum: 99,
		Buckets: []histogram.Bucket{{Upper: histogram.InfBound(), CumulativeCount: 2}},
	}
	mustValidate(t, h)
	e, err := histogram.EstimateQuantile(h, 0.5)
	if err != nil {
		t.Fatal(err)
	}
	if e.Point != nil || e.Lower != nil || e.Upper != nil {
		t.Fatalf("interval should be (-Inf,+Inf) with no point, got %+v", e)
	}
}

func TestQuantile_RankInInfBucketAfterFinite(t *testing.T) {
	// 2 observations <=1, 1 observation >10: q=0.9 (rank 2.7) -> +Inf bucket.
	h := cum(30, []float64{1, 10}, 2, 0, 1)
	mustValidate(t, h)
	e, err := histogram.EstimateQuantile(h, 0.9)
	if err != nil {
		t.Fatal(err)
	}
	if e.Point != nil {
		t.Fatalf("no point estimate possible in open +Inf bucket, got %v", *e.Point)
	}
	if e.Lower == nil || *e.Lower != 10 {
		t.Fatalf("lower edge should be 10, got %v", e.Lower)
	}
	if e.Upper != nil {
		t.Fatalf("upper edge should be +Inf(nil), got %v", *e.Upper)
	}
}

func TestQuantile_Q1_LowerEdgeOfInf(t *testing.T) {
	h := cum(30, []float64{1, 10}, 2, 0, 1)
	e, err := histogram.EstimateQuantile(h, 1)
	if err != nil {
		t.Fatal(err)
	}
	if e.Point == nil || *e.Point != 10 {
		t.Fatalf("q=1 point should be 10 (lower edge of +Inf), got %v", e.Point)
	}
	if e.Upper != nil {
		t.Fatal("q=1 upper should be +Inf")
	}
}

// finite quantile interval ---------------------------------------------------

func TestQuantile_IntervalBracketsPoint(t *testing.T) {
	// 1 <=1, 1 in (1,10], 1 in (10,+Inf]: total 3.
	h := cum(15, []float64{1, 10}, 1, 1, 1)
	mustValidate(t, h)
	e, err := histogram.EstimateQuantile(h, 0.5) // rank 1.5
	if err != nil {
		t.Fatal(err)
	}
	if e.Point == nil {
		t.Fatal("expected interpolated point")
	}
	// rank 1.5 inside bucket (1,10], counts 1 -> 2: point = 1+9*0.5=5.5
	if math.Abs(*e.Point-5.5) > 1e-9 {
		t.Fatalf("point = %v, want 5.5", *e.Point)
	}
	if e.Lower == nil || *e.Lower != 1 || e.Upper == nil || *e.Upper != 10 {
		t.Fatalf("interval should be [1,10], got [%v,%v]", e.Lower, e.Upper)
	}
	if !(*e.Lower <= *e.Point && *e.Point <= *e.Upper) {
		t.Fatal("point must be inside the interval")
	}
}

func TestQuantile_Q0_FirstBucket(t *testing.T) {
	h := cum(15, []float64{1, 10}, 2, 1, 0)
	e, err := histogram.EstimateQuantile(h, 0)
	if err != nil {
		t.Fatal(err)
	}
	if e.Lower != nil {
		t.Fatalf("q=0 lower must be -Inf(nil), got %v", *e.Lower)
	}
	if e.Upper == nil || *e.Upper != 1 {
		t.Fatalf("q=0 upper must be 1, got %v", e.Upper)
	}
}

func TestQuantile_InvalidQ(t *testing.T) {
	h := cum(1, []float64{1}, 1, 0)
	for _, q := range []float64{-0.1, 1.1, math.NaN()} {
		if _, err := histogram.EstimateQuantile(h, q); !errors.Is(err, histogram.ErrInvalidQuantile) {
			t.Fatalf("q=%v: want ErrInvalidQuantile, got %v", q, err)
		}
	}
}

// error cumulative values ----------------------------------------------------

func TestIngestStyleCounterResetDetected(t *testing.T) {
	// Simulate what store does at the histogram level: a later cumulative
	// sample must dominate the earlier one bucket-wise.
	first := cum(10, []float64{1, 2}, 1, 2, 3)
	later := cum(11, []float64{1, 2}, 0, 4, 4) // bucket<=1 decreased 1->0
	mustValidate(t, first)
	mustValidate(t, later)
	reset := false
	for _, pb := range first.Buckets {
		if c, ok := later.CountAt(pb.Upper); ok && c < pb.CumulativeCount {
			reset = true
		}
	}
	if !reset {
		t.Fatal("bucket cumulative decrease must be detected as counter reset")
	}
}
