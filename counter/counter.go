// Package counter implements interval increase and rate estimation for
// monotonic counters observed as discrete samples.
//
// It is the pure domain core: no I/O, no HTTP. The rules implemented here are
// deliberate and are documented in README §"计算规则与语义" (Rules and semantics):
//
//   - samples are sorted by timestamp; equal timestamps and non-finite values
//     are rejected by the caller before analysis;
//   - a non-negative value delta between two consecutive samples is the observed
//     increase for that interval;
//   - a negative delta is a counter reset: increase is estimated as the
//     post-reset value (the Prometheus convention total = last-first plus the
//     pre-reset value at each reset; per pair that is just b);

//   - an interval whose duration exceeds the gap threshold is "missing samples"
//     and contributes nothing (no interpolation across unobserved interiors);
//   - only the boundary intervals that straddle the query window start/end are
//     linearly interpolated; the leading and trailing unobserved regions are
//     never extrapolated;
//   - every estimate is reported together with an honest [min, max] range:
//     interpolation assumes linearity (min=0, max=observed delta), a reset
//     straddled by a boundary contributes min=0.
package counter

import (
	"fmt"
	"math"
	"sort"
	"strings"
)

// Sample is one counter observation: a floating point counter value at time t.
type Sample struct {
	T     float64
	Value float64
}

// ValidateSample checks one observation. Timestamps are in seconds (any
// numeric unit works as long as it is consistent) and values must be finite
// and non-negative because counters are monotonic and non-negative.
func ValidateSample(s Sample) error {
	if math.IsNaN(s.T) || math.IsInf(s.T, 0) {
		return fmt.Errorf("timestamp must be finite")
	}
	if math.IsNaN(s.Value) || math.IsInf(s.Value, 0) {
		return fmt.Errorf("counter value at t=%v must be finite", s.T)
	}
	if s.Value < 0 {
		return fmt.Errorf("counter value at t=%v is negative (%v): counters are monotonic, negatives are rejected", s.T, s.Value)
	}
	return nil
}

// Segment is the analysis of one interval between two consecutive observed
// samples, clipped against the query window.
type Segment struct {
	From        float64 `json:"from"`
	To          float64 `json:"to"`
	Duration    float64 `json:"duration"`
	FromValue   float64 `json:"from_value"`
	ToValue     float64 `json:"to_value"`
	Increase    float64 `json:"increase"`
	MinIncrease float64 `json:"min_increase"`
	MaxIncrease float64 `json:"max_increase"`
	// Kind is "observed", "interpolated" or "gap" (unobserved due to missing
	// samples; contributes no increase).
	Kind string `json:"kind"`
	// Reset marks intervals where the raw value went backwards: the counter
	// restarted from zero (or a smaller number).
	Reset bool `json:"reset,omitempty"`
	// Boundary is "start" or "end" when this interval was clipped by the query
	// window and its increase was obtained by linear interpolation.
	Boundary string `json:"boundary,omitempty"`
	// InterpolatedAcrossReset is true when a boundary cut falls inside an
	// interval containing a reset: linear interpolation is undefined there, so
	// the estimate is 0 with min=0,max=raw post-reset increase.
	InterpolatedAcrossReset bool `json:"interpolated_across_reset,omitempty"`
	// Missing marks an interval longer than the gap threshold: we know nothing
	// about its interior and do not guess.
	Missing bool `json:"missing,omitempty"`
}

// WindowResult is the full answer for one series over one query window.
type WindowResult struct {
	Metric     string            `json:"metric"`
	Labels     map[string]string `json:"labels"`
	WindowFrom float64           `json:"window_from"`
	WindowTo   float64           `json:"window_to"`

	Increase    float64   `json:"increase"`
	MinIncrease float64   `json:"min_increase"`
	MaxIncrease float64   `json:"max_increase"`
	Resets      []Reset   `json:"resets"`
	Segments    []Segment `json:"segments"`

	// Durations in seconds.
	WindowDuration   float64 `json:"window_duration"`
	ObservedDuration float64 `json:"observed_duration"`
	UnobservedBefore float64 `json:"unobserved_before"`
	UnobservedAfter  float64 `json:"unobserved_after"`
	GapsDuration     float64 `json:"gaps_duration"`
	Coverage         float64 `json:"coverage"`
	HasData          bool    `json:"has_data"`

	// Rates are increase divided by duration, per second. Null (nil pointer)
	// when undefined. See README for which denominator is used.
	WindowRate   *Rate `json:"window_rate"`
	ObservedRate *Rate `json:"observed_rate"`

	MissingSamples int `json:"missing_samples"`

	// Notes is machine-readable caveats; NotesText joins them in English.
	Notes     []string `json:"notes"`
	NotesText string   `json:"notes_text"`
}

// Reset records where and how a counter reset was observed.
type Reset struct {
	At        float64 `json:"at"`
	FromValue float64 `json:"from_value"`
	ToValue   float64 `json:"to_value"`
	Increase  float64 `json:"increase"`
}

// Rate is an increase per second with an honest range.
type Rate struct {
	PerSecond float64 `json:"per_second"`
	Min       float64 `json:"min_per_second"`
	Max       float64 `json:"max_per_second"`
}

// AnalysisOptions controls windowing and gap detection.
type AnalysisOptions struct {
	From, To float64
	// MaxInterval marks consecutive samples farther apart than this as a gap
	// (missing samples). If <= 0 it is derived as 1.5x the median interval.
	MaxInterval float64
}

// Analyze computes window increase and rate for one sorted, validated sample
// list. It never mutates its inputs. Empty windows return a WindowResult with
// HasData=false and null rates.
func Analyze(metric string, labels map[string]string, samples []Sample, opts AnalysisOptions) WindowResult {
	if opts.To < opts.From {
		opts.To = opts.From
	}
	res := WindowResult{
		Metric:         metric,
		Labels:         cloneLabels(labels),
		WindowFrom:     opts.From,
		WindowTo:       opts.To,
		WindowDuration: opts.To - opts.From,
		Segments:       []Segment{},
		Resets:         []Reset{},
		Notes:          []string{},
	}

	sorted := make([]Sample, len(samples))
	copy(sorted, samples)
	sort.SliceStable(sorted, func(i, j int) bool { return sorted[i].T < sorted[j].T })

	// Keep only samples at or after the window start; we also keep the nearest
	// earlier sample because it anchors the start-boundary interpolation.
	i0 := sort.Search(len(sorted), func(i int) bool { return sorted[i].T >= opts.From })
	if i0 > 0 {
		i0--
	}
	in := sorted[i0:]

	gapThreshold := opts.MaxInterval
	if gapThreshold <= 0 && len(in) >= 2 {
		gapThreshold = 1.5 * medianInterval(in)
	}

	var incEst, incMin, incMax float64
	var observedDur float64

	for i := 1; i < len(in); i++ {
		a, b := in[i-1], in[i]
		if b.T <= opts.From || a.T >= opts.To {
			continue // interval does not intersect the window
		}

		lo := math.Max(a.T, opts.From)
		hi := math.Min(b.T, opts.To)
		dur := hi - lo
		if dur <= 0 {
			continue
		}

		rawDelta := b.Value - a.Value
		isReset := rawDelta < 0
		// Prometheus convention (confirmed against promql/functions.go):
		// total increase = (last-first) + sum(prev value at each reset).
		// Per pair that means a reset pair contributes the post-reset value b;
		// the old-era increase between the last observation and the reset
		// instant is conservatively taken as zero (it is unknowable).
		pairIncrease := rawDelta
		if isReset {
			pairIncrease = b.Value
		}
		isGap := gapThreshold > 0 && (b.T-a.T) > gapThreshold

		seg := Segment{
			From: lo, To: hi, Duration: dur,
			FromValue: a.Value, ToValue: b.Value,
		}

		switch {
		case isGap:
			// The interior is unobserved: contribute nothing, make no claim.
			seg.Kind, seg.Missing = "gap", true
			res.GapsDuration += dur
			res.MissingSamples++
			res.Notes = append(res.Notes, fmt.Sprintf("missing_samples:%v->%v", a.T, b.T))
		case lo == a.T && hi == b.T:
			// Fully observed interval inside (or exactly spanning) the window.
			seg.Kind = "observed"
			if isReset {
				seg.Reset = true
				res.Resets = append(res.Resets, Reset{At: b.T, FromValue: a.Value, ToValue: b.Value, Increase: pairIncrease})
			}
			incEst += pairIncrease
			incMin += pairIncrease
			incMax += pairIncrease
			observedDur += dur
		default:
			// Interval straddles a window boundary: linear interpolation.
			seg.Kind = "interpolated"
			f := dur / (b.T - a.T)
			if lo > a.T {
				seg.Boundary = "start"
			} else {
				seg.Boundary = "end"
			}
			if isReset {
				// We do not know where within the interval the reset happened,
				// so linear interpolation is not defensible: estimate 0 in the
				// clipped part with honest bounds [0, pairIncrease].
				seg.Reset = true
				seg.InterpolatedAcrossReset = true
				seg.Increase, seg.MinIncrease, seg.MaxIncrease = 0, 0, pairIncrease
				incMax += pairIncrease
				res.Resets = append(res.Resets, Reset{At: b.T, FromValue: a.Value, ToValue: b.Value, Increase: pairIncrease})
				res.Notes = append(res.Notes, fmt.Sprintf("interpolation_across_reset:%v", b.T))
			} else {
				v := f * pairIncrease
				seg.Increase, seg.MinIncrease, seg.MaxIncrease = v, 0, pairIncrease
				incEst += v
				incMax += pairIncrease
			}
			observedDur += dur
		}
		res.Segments = append(res.Segments, seg)
	}

	// Leading/trailing unobserved regions (reported, never extrapolated).
	if len(in) > 0 {
		first, last := in[0], in[len(in)-1]
		if first.T > opts.From {
			res.UnobservedBefore = math.Min(first.T, opts.To) - opts.From
			if res.UnobservedBefore < 0 {
				res.UnobservedBefore = 0
			}
		}
		if last.T < opts.To {
			res.UnobservedAfter = opts.To - math.Max(last.T, opts.From)
			if res.UnobservedAfter < 0 {
				res.UnobservedAfter = 0
			}
		}
	} else {
		res.UnobservedBefore = res.WindowDuration
	}

	res.Increase, res.MinIncrease, res.MaxIncrease = incEst, incMin, incMax
	res.ObservedDuration = observedDur
	if res.WindowDuration > 0 {
		res.Coverage = observedDur / res.WindowDuration
	}
	res.HasData = observedDur > 0 || len(res.Segments) > 0

	if len(in) == 0 {
		res.HasData = false
		res.Notes = append(res.Notes, "no_samples_in_window")
	}
	if res.UnobservedBefore > 0 {
		res.Notes = append(res.Notes, "unobserved_leading_region:not_extrapolated")
	}
	if res.UnobservedAfter > 0 {
		res.Notes = append(res.Notes, "unobserved_trailing_region:not_extrapolated")
	}
	if res.MissingSamples > 0 {
		res.Notes = append(res.Notes, "missing_intervals_excluded")
	}
	hasInterp := false
	for _, s := range res.Segments {
		if s.Kind == "interpolated" {
			hasInterp = true
		}
	}
	if hasInterp {
		res.Notes = append(res.Notes, "boundary_linearly_interpolated")
	}
	if len(res.Resets) > 0 {
		res.Notes = append(res.Notes, fmt.Sprintf("resets_detected:%d", len(res.Resets)))
	}

	if observedDur > 0 {
		res.WindowRate = &Rate{
			PerSecond: incEst / res.WindowDuration,
			Min:       incMin / res.WindowDuration,
			Max:       incMax / res.WindowDuration,
		}
		res.ObservedRate = &Rate{
			PerSecond: incEst / observedDur,
			Min:       incMin / observedDur,
			Max:       incMax / observedDur,
		}
	}

	res.NotesText = strings.Join(res.Notes, "; ")
	return res
}

func medianInterval(s []Sample) float64 {
	d := make([]float64, 0, len(s)-1)
	for i := 1; i < len(s); i++ {
		if g := s[i].T - s[i-1].T; g > 0 {
			d = append(d, g)
		}
	}
	sort.Float64s(d)
	if len(d) == 0 {
		return 0
	}
	m := len(d) / 2
	if len(d)%2 == 1 {
		return d[m]
	}
	return (d[m-1] + d[m]) / 2
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
