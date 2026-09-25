package synthetic_test

import (
	"math"
	"testing"
	"time"

	"histmerge/internal/histogram"
	"histmerge/internal/store"
	"histmerge/internal/synthetic"
)

// TestSyntheticMerge_ConservationAcrossLayouts ingests the generated samples
// (mixed fine/coarse layouts), aggregates the latest sample per series via
// coarsened merge, and verifies:
//   - merged total equals the sum of input totals (count conservation);
//   - every retained bucket equals the sum of input counts at that bound;
//   - every finite quantile point lies within its reported interval.
func TestSyntheticMerge_ConservationAcrossLayouts(t *testing.T) {
	samples, err := synthetic.Generate(time.Date(2026, 9, 24, 0, 0, 0, 0, time.UTC), 4, 7)
	if err != nil {
		t.Fatal(err)
	}
	// Latest sample per series key.
	latest := map[string]*histogram.Histogram{}
	order := []string{}
	for _, s := range samples {
		key := labelKey(s.Labels)
		if _, ok := latest[key]; !ok {
			order = append(order, key)
		}
		latest[key] = s.Histogram // samples are generated in time order
	}
	hs := make([]*histogram.Histogram, 0, len(order))
	var inputTotal uint64
	for _, k := range order {
		hs = append(hs, latest[k])
		inputTotal += latest[k].TotalCount
	}

	res, err := histogram.Merge(hs...)
	if err != nil {
		t.Fatalf("merge: %v", err)
	}
	if res.Merged.TotalCount != inputTotal {
		t.Fatalf("total not conserved: merged %d != inputs %d", res.Merged.TotalCount, inputTotal)
	}
	if !res.Conserved || !res.BucketConserved {
		t.Fatalf("conservation flags: conserved=%v bucket=%v", res.Conserved, res.BucketConserved)
	}

	// Quantile points must sit inside their intervals.
	est, err := histogram.EstimateQuantiles(res.Merged, []float64{0.01, 0.1, 0.25, 0.5, 0.75, 0.9, 0.99})
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range est {
		if e.Point == nil {
			continue // open +Inf bucket: no point, interval is the honest answer
		}
		if e.Lower != nil && *e.Point < *e.Lower-1e-12 {
			t.Fatalf("q=%v point %v below lower %v", e.Q, *e.Point, *e.Lower)
		}
		if e.Upper != nil && *e.Point > *e.Upper+1e-12 {
			t.Fatalf("q=%v point %v above upper %v", e.Q, *e.Point, *e.Upper)
		}
		if math.IsNaN(*e.Point) {
			t.Fatalf("q=%v point is NaN", e.Q)
		}
	}
}

// TestSynthetic_AllGeneratedHistogramsValid validates every generated sample.
func TestSynthetic_AllGeneratedHistogramsValid(t *testing.T) {
	samples, err := synthetic.Generate(time.Now().UTC(), 3, 1)
	if err != nil {
		t.Fatal(err)
	}
	if len(samples) == 0 {
		t.Fatal("no samples generated")
	}
	// Every series' cumulative counts must be non-decreasing over scrapes.
	bySeries := map[string][]uint64{}
	for _, s := range samples {
		if err := s.Histogram.Validate(); err != nil {
			t.Fatalf("generated histogram invalid: %v", err)
		}
		k := labelKey(s.Labels)
		prev := bySeries[k]
		for i, bk := range s.Histogram.Buckets {
			if i < len(prev) && bk.CumulativeCount < prev[i] {
				t.Fatalf("series %s bucket %d decreased across scrapes: %d -> %d",
					k, i, prev[i], bk.CumulativeCount)
			}
		}
		bySeries[k] = bucketCounts(s.Histogram)
	}
}

// TestSynthetic_EmptySampleIsValid covers the empty-histogram acceptance case.
func TestSynthetic_EmptySampleIsValid(t *testing.T) {
	s := synthetic.EmptySample(time.Now().UTC(), "idle")
	if err := s.Histogram.Validate(); err != nil {
		t.Fatalf("empty sample invalid: %v", err)
	}
	if s.Histogram.TotalCount != 0 {
		t.Fatal("empty sample must have zero total")
	}
}

func bucketCounts(h *histogram.Histogram) []uint64 {
	out := make([]uint64, len(h.Buckets))
	for i, bk := range h.Buckets {
		out[i] = bk.CumulativeCount
	}
	return out
}

func labelKey(ls []store.Label) string {
	out := ""
	for _, l := range ls {
		out += l.Name + "=" + l.Value + ","
	}
	return out
}
