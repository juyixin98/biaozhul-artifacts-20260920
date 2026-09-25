// Package rollup contains the bucket alignment and aggregation math shared by
// every retention layer. Aggregates always carry count/sum/min/max so that
// layers can be merged exactly; averages are never re-averaged.
package rollup

import "math"

// Width of a retention layer, in seconds.
const (
	Second = 1
	Minute = 60
	Hour   = 3600
)

// Agg is a partially or fully aggregated bucket.
//
// Count is the number of raw samples represented (0 marks an empty bucket).
// Sum/Min/Max are over those raw values. The mean is derivable as Sum/Count
// and is deliberately not stored: storing only a mean would make exact
// roll-up into coarser layers impossible.
type Agg struct {
	Count int64   `json:"count"`
	Sum   float64 `json:"sum"`
	Min   float64 `json:"min"`
	Max   float64 `json:"max"`
}

// Empty reports whether the bucket holds no samples.
func (a Agg) Empty() bool { return a.Count == 0 }

// Mean returns the arithmetic mean or 0 for an empty bucket.
func (a Agg) Mean() float64 {
	if a.Count == 0 {
		return 0
	}
	return a.Sum / float64(a.Count)
}

// AlignStart floors t to the most recent bucket boundary of width w.
// w must divide the parent width for every layer; Second/Minute/Hour do.
func AlignStart(t int64, w int64) int64 {
	if t >= 0 {
		return t - t%w
	}
	// Negative timestamps are not expected from Unix clocks, but floor
	// correctly anyway.
	return t - ((t%w)+w)%w
}

// FromSample builds a single-sample aggregate.
func FromSample(v float64) Agg {
	return Agg{Count: 1, Sum: v, Min: v, Max: v}
}

// Merge combines two aggregates over disjoint (or even overlapping) sets of
// raw samples. Counts and sums add; min/max take extremes. This is the only
// operation used between layers, so a minute bucket is built from raw
// samples and an hour bucket is built from minute buckets — never from
// stored means.
func Merge(a, b Agg) Agg {
	if a.Count == 0 {
		return b
	}
	if b.Count == 0 {
		return a
	}
	return Agg{
		Count: a.Count + b.Count,
		Sum:   a.Sum + b.Sum,
		Min:   math.Min(a.Min, b.Min),
		Max:   math.Max(a.Max, b.Max),
	}
}
