// Package synthetic produces deterministic sample streams for demos and
// tests. Nothing here talks to a real monitoring system.
package synthetic

import (
	"math"

	"metricrollup/internal/model"
)

// Options controls one synthetic run.
type Options struct {
	Metric string
	Labels map[string]string
	// Start/End are Unix seconds; samples are generated on [Start, End).
	Start int64
	End   int64
	// PeriodSec is the base sampling period in seconds.
	PeriodSec int64
	// Amplitude and Offset shape the sine signal.
	Amplitude float64
	Offset    float64
	// JitterSec, when > 0, shifts each sample by a deterministic pseudo
	// random offset in [0, JitterSec), producing irregular density and
	// cross-boundary placement.
	JitterSec int64
	// Seed varies the deterministic pseudo-random sequence.
	Seed int64
	// GapEvery, when > 0, skips the nth sample to create empty buckets.
	GapEvery int64
}

// Generate returns samples according to opts. Timestamps and values are
// deterministic for the same inputs.
func Generate(opts Options) []model.Sample {
	if opts.PeriodSec <= 0 || opts.End <= opts.Start {
		return nil
	}
	out := make([]model.Sample, 0, (opts.End-opts.Start)/opts.PeriodSec+1)
	n := int64(0)
	for t := opts.Start; t < opts.End; t += opts.PeriodSec {
		if opts.GapEvery > 0 && n > 0 && n%opts.GapEvery == 0 {
			n++
			continue
		}
		ts := t
		if opts.JitterSec > 1 {
			ts += rand(n, opts.Seed) % opts.JitterSec
		}
		phase := float64(ts-opts.Start) / 60.0 // one sine cycle per ~6.28 min
		v := opts.Offset + opts.Amplitude*math.Sin(phase) + 0.5*randFloat(n+777, opts.Seed)
		out = append(out, model.Sample{
			Metric: opts.Metric,
			Labels: cloneLabels(opts.Labels),
			Ts:     ts,
			// Round keeps fixture output readable; aggregation math does
			// not depend on it.
			Value: math.Round(v*1000) / 1000,
		})
		n++
	}
	return out
}

// rand is a small deterministic hash so generator output needs no rand seed
// management and never changes between Go versions.
func rand(n, seed int64) int64 {
	x := uint64(n)*6364136223846793005 + 1442695040888963407 + uint64(seed)*1140071481932319848
	x ^= x >> 33
	x *= 0xff51afd7ed558ccd
	x ^= x >> 33
	return int64(x >> 1) // top bit dropped to stay in int64 range
}

func randFloat(n, seed int64) float64 {
	return float64(rand(n, seed)%10000) / 10000.0
}

func cloneLabels(in map[string]string) map[string]string {
	if len(in) == 0 {
		return map[string]string{}
	}
	out := make(map[string]string, len(in))
	for k, v := range in {
		out[k] = v
	}
	return out
}
