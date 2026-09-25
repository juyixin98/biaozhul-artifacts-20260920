// Package synth generates synthetic cumulative histograms so the backend
// can be exercised without a real monitoring platform.
package synth

import (
	"math"
	"math/rand"

	"histmerge/internal/histogram"
)

// LatencyBounds are the default bucket upper bounds (seconds), +Inf implied at the end.
var LatencyBounds = []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10}

// CoarserBounds is every other bound of LatencyBounds — used to demonstrate
// merging histograms with different (but compatible) boundary sets.
var CoarserBounds = []float64{0.01, 0.05, 0.1, 0.5, 2.5, 10}

// Generate builds a synthetic histogram of n observations whose values are
// log-normally distributed around the given median (seconds). Every bounds
// slice gets a +Inf appended automatically.
func Generate(rng *rand.Rand, name string, bounds []float64, median float64, n int) *histogram.Histogram {
	full := append(append([]float64(nil), bounds...), math.Inf(1))
	counts := make([]uint64, len(full))
	for i := 0; i < n; i++ {
		v := median * math.Exp(rng.NormFloat64()*0.6)
		j := 0
		for j < len(full) && v > full[j] {
			j++
		}
		if j == len(full) {
			j = len(full) - 1
		}
		counts[j]++
	}
	// per-bucket -> cumulative
	for i := 1; i < len(counts); i++ {
		counts[i] += counts[i-1]
	}
	return &histogram.Histogram{Name: name, Bounds: full, Counts: counts}
}
