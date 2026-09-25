// Package verify computes independent ground-truth aggregates straight from
// raw samples and compares them against store query results. It is the
// acceptance harness for "incremental roll-up matches recomputation".
package verify

import (
	"fmt"
	"math"

	"metricrollup/internal/model"
	"metricrollup/internal/rollup"
)

// ReferenceBucket is a ground-truth bucket recomputed from raw samples.
type ReferenceBucket struct {
	Start int64
	Agg   rollup.Agg // Count == 0 for an empty bucket
}

// ReferenceAggregate groups raw samples into width-second aligned buckets
// over [start, end). Empty buckets are included. This is the independent
// oracle: it ignores every rolled-up layer and walks the raw points.
func ReferenceAggregate(samples []model.Sample, start, end, width int64) []ReferenceBucket {
	first := rollup.AlignStart(start, width)
	last := rollup.AlignStart(end-1, width)
	byStart := map[int64]rollup.Agg{}
	for _, smp := range samples {
		if smp.Ts < start || smp.Ts >= end {
			continue
		}
		b := rollup.AlignStart(smp.Ts, width)
		byStart[b] = rollup.Merge(byStart[b], rollup.FromSample(smp.Value))
	}
	out := make([]ReferenceBucket, 0, (last-first)/width+1)
	for b := first; b <= last; b += width {
		out = append(out, ReferenceBucket{Start: b, Agg: byStart[b]})
	}
	return out
}

// Tol is the floating-point comparison tolerance. Sums differ by no more
// than accumulated IEEE-754 error; 1e-9 relative is ample for the fixtures
// and far below any real signal difference.
const Tol = 1e-9

// Mismatch describes one bucket where incremental and reference disagree.
type Mismatch struct {
	Start  int64
	Field  string
	Got    float64
	Expect float64
}

func (m Mismatch) String() string {
	return fmt.Sprintf("bucket starting %d: %s incremental=%v reference=%v", m.Start, m.Field, m.Got, m.Expect)
}

// Compare checks counts and sum/min/max between store-style buckets and the
// reference oracle. Means are intentionally not compared here: they are a
// presentation field derived from sum/count.
func Compare(got []GotBucket, want []ReferenceBucket) []Mismatch {
	var mismatches []Mismatch
	if len(got) != len(want) {
		mismatches = append(mismatches, Mismatch{
			Field:  "bucket_count",
			Got:    float64(len(got)),
			Expect: float64(len(want)),
		})
		return mismatches
	}
	for i := range want {
		if got[i].Start != want[i].Start {
			mismatches = append(mismatches, Mismatch{
				Start:  want[i].Start,
				Field:  "start",
				Got:    float64(got[i].Start),
				Expect: float64(want[i].Start),
			})
			continue
		}
		g, w := got[i].Count, want[i].Agg.Count
		if g != w {
			mismatches = append(mismatches, Mismatch{Start: want[i].Start, Field: "count", Got: float64(g), Expect: float64(w)})
		}
		if w == 0 {
			continue // empty bucket: numeric fields are absent by design
		}
		if !approx(got[i].Sum, want[i].Agg.Sum) {
			mismatches = append(mismatches, Mismatch{Start: want[i].Start, Field: "sum", Got: got[i].Sum, Expect: want[i].Agg.Sum})
		}
		if !approx(got[i].Min, want[i].Agg.Min) {
			mismatches = append(mismatches, Mismatch{Start: want[i].Start, Field: "min", Got: got[i].Min, Expect: want[i].Agg.Min})
		}
		if !approx(got[i].Max, want[i].Agg.Max) {
			mismatches = append(mismatches, Mismatch{Start: want[i].Start, Field: "max", Got: got[i].Max, Expect: want[i].Agg.Max})
		}
	}
	return mismatches
}

// GotBucket is the store-side shape the comparator accepts, so the verify
// package does not import the store package (store already imports model).
type GotBucket struct {
	Start int64
	Count int64
	Sum   float64
	Min   float64
	Max   float64
}

func approx(a, b float64) bool {
	if math.IsNaN(a) || math.IsNaN(b) {
		return false
	}
	diff := math.Abs(a - b)
	scale := math.Max(1, math.Max(math.Abs(a), math.Abs(b)))
	return diff <= Tol*scale
}
