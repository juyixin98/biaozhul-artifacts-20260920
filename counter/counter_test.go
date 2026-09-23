package counter

import (
	"math"
	"testing"
)

func approx(t *testing.T, name string, got, want float64) {
	t.Helper()
	if math.Abs(got-want) > 1e-9 {
		t.Errorf("%s = %v, want %v", name, got, want)
	}
}

// acceptanceSeries is the README hand-computed sequence with one gap and one
// reset. Kept in sync with seed.MainSeries.
func acceptanceSeries() []Sample {
	return []Sample{
		{T: 0, Value: 10},
		{T: 60, Value: 20},
		{T: 120, Value: 30},
		{T: 180, Value: 40},
		// t=240 missing on purpose: 180->300 is a 120s gap vs 60s cadence.
		{T: 300, Value: 70},
		{T: 360, Value: 80},
		{T: 420, Value: 90},
		{T: 480, Value: 100},
		{T: 540, Value: 5}, // reset 100 -> 5
		{T: 600, Value: 15},
	}
}

// TestHandComputedFullWindow reproduces the README hand calculation over the
// full window. Intervals are 60s apart except the 120s gap; median=60 so the
// auto gap threshold is 90s and 180->300 is excluded.
func TestHandComputedFullWindow(t *testing.T) {
	res := Analyze("requests_total", nil, acceptanceSeries(), AnalysisOptions{From: 0, To: 600})

	// Observed pairs: seven plain +10 pairs (70) plus the reset pair, which by
	// the Prometheus convention contributes the post-reset value 5.
	approx(t, "increase", res.Increase, 75)
	approx(t, "min_increase", res.MinIncrease, 75)
	approx(t, "max_increase", res.MaxIncrease, 75)
	approx(t, "observed_duration", res.ObservedDuration, 480)
	approx(t, "window_duration", res.WindowDuration, 600)
	approx(t, "gaps_duration", res.GapsDuration, 120)
	approx(t, "coverage", res.Coverage, 0.8)
	approx(t, "window rate", res.WindowRate.PerSecond, 0.125)
	approx(t, "observed rate", res.ObservedRate.PerSecond, 0.15625)

	if len(res.Resets) != 1 {
		t.Fatalf("resets = %d, want 1", len(res.Resets))
	}
	approx(t, "reset at", res.Resets[0].At, 540)
	approx(t, "reset contribution", res.Resets[0].Increase, 5)
	if res.MissingSamples != 1 {
		t.Errorf("missing_samples = %d, want 1", res.MissingSamples)
	}
	if !res.HasData {
		t.Errorf("expected has_data")
	}
}

// TestHandComputedMultipleResets is the second worked example in the README:
//
//	100 -> 200 -> [reset] 5 -> 10 -> [reset] 2 -> 8
//
// Plain: +100, reset pair contributes 5, +5, reset pair contributes 2, +6.
// Total = 118, which also equals (8-100) + 200 + 10 = 118.
func TestHandComputedMultipleResets(t *testing.T) {
	s := []Sample{
		{T: 0, Value: 100}, {T: 10, Value: 200}, {T: 20, Value: 5},
		{T: 30, Value: 10}, {T: 40, Value: 2}, {T: 50, Value: 8},
	}
	res := Analyze("x", nil, s, AnalysisOptions{From: 0, To: 50, MaxInterval: 1000})
	approx(t, "increase", res.Increase, 118)
	if len(res.Resets) != 2 {
		t.Fatalf("resets = %d, want 2", len(res.Resets))
	}
}

// TestBoundaryInterpolation cuts the acceptance series at [30,570] so both
// boundary pairs are linearly interpolated at 50%.
func TestBoundaryInterpolation(t *testing.T) {
	res := Analyze("requests_total", nil, acceptanceSeries(), AnalysisOptions{From: 30, To: 570})

	// Exact interior: five +10 pairs + reset pair 5 = 55.
	// Start half of 0->60 (+10): estimate 5, range [0,10].
	// End   half of 5->15 (+10): estimate 5, range [0,10].
	approx(t, "increase", res.Increase, 65)
	approx(t, "min_increase", res.MinIncrease, 55)
	approx(t, "max_increase", res.MaxIncrease, 75)
	approx(t, "observed_duration", res.ObservedDuration, 420) // 30 + 6*60 + 30
	approx(t, "gaps", res.GapsDuration, 120)

	var nStart, nEnd int
	for _, seg := range res.Segments {
		if seg.Kind == "interpolated" {
			if seg.Boundary == "start" {
				nStart++
				approx(t, "start seg inc", seg.Increase, 5)
			}
			if seg.Boundary == "end" {
				nEnd++
				approx(t, "end seg inc", seg.Increase, 5)
			}
		}
	}
	if nStart != 1 || nEnd != 1 {
		t.Errorf("interpolated boundaries start=%d end=%d, want 1/1", nStart, nEnd)
	}
}

// TestBoundaryAcrossReset: ending the window at 510, inside the 480->540 reset
// pair. Linear interpolation is not defensible across an unknown reset point,
// so the clipped part estimates 0 with max 5.
func TestBoundaryAcrossReset(t *testing.T) {
	res := Analyze("requests_total", nil, acceptanceSeries(), AnalysisOptions{From: 0, To: 510})
	approx(t, "increase", res.Increase, 60) // 30 + 30 + 0
	approx(t, "min_increase", res.MinIncrease, 60)
	approx(t, "max_increase", res.MaxIncrease, 65)
	var flagged bool
	for _, seg := range res.Segments {
		if seg.InterpolatedAcrossReset {
			flagged = true
			approx(t, "cross-reset seg inc", seg.Increase, 0)
			approx(t, "cross-reset seg max", seg.MaxIncrease, 5)
		}
	}
	if !flagged {
		t.Errorf("expected an interpolation_across_reset segment")
	}
}

// TestNoExtrapolation: samples stop at 600; a query to 720 must report a
// trailing unobserved region and never extrapolate growth into it.
func TestNoExtrapolation(t *testing.T) {
	res := Analyze("requests_total", nil, acceptanceSeries(), AnalysisOptions{From: 600, To: 720})
	if res.HasData {
		t.Errorf("window with no interval should have has_data=false")
	}
	approx(t, "unobserved_after", res.UnobservedAfter, 120)
	if res.Increase != 0 || res.WindowRate != nil {
		t.Errorf("expected zero increase and null rate beyond observations, got inc=%v rate=%v", res.Increase, res.WindowRate)
	}
}

// TestEmptyWindow has no samples at all: full window reported unobserved,
// nothing guessed.
func TestEmptyWindow(t *testing.T) {
	res := Analyze("x", nil, nil, AnalysisOptions{From: 0, To: 10})
	if res.HasData {
		t.Errorf("empty input must be has_data=false")
	}
	approx(t, "unobserved_before", res.UnobservedBefore, 10)
	if res.WindowRate != nil {
		t.Errorf("rate must be null without data")
	}
}

// TestOutOfOrderNormalized: shuffled input gives identical results.
func TestOutOfOrderNormalized(t *testing.T) {
	sorted := []Sample{
		{T: 0, Value: 0}, {T: 60, Value: 2}, {T: 120, Value: 3},
		{T: 180, Value: 6}, {T: 240, Value: 1},
	}
	shuffled := []Sample{sorted[3], sorted[0], sorted[4], sorted[1], sorted[2]}
	a := Analyze("e", nil, sorted, AnalysisOptions{From: 0, To: 240, MaxInterval: 1000})
	b := Analyze("e", nil, shuffled, AnalysisOptions{From: 0, To: 240, MaxInterval: 1000})
	if a.Increase != b.Increase || len(a.Resets) != len(b.Resets) || len(a.Segments) != len(b.Segments) {
		t.Errorf("out-of-order results differ: %+v vs %+v", a, b)
	}
}

// TestGapExcludedViaExplicitThreshold marks the 180->300 interval a gap with
// an explicit threshold and proves nothing is attributed across it.
func TestGapExcludedViaExplicitThreshold(t *testing.T) {
	// Threshold 90: same as the auto median-based rule.
	res := Analyze("requests_total", nil, acceptanceSeries(), AnalysisOptions{From: 0, To: 600, MaxInterval: 90})
	approx(t, "increase", res.Increase, 75)
	if res.MissingSamples != 1 {
		t.Fatalf("expected one gap, got %d", res.MissingSamples)
	}
	// Threshold 600: nothing is a gap; then the 120s interval is a plain
	// observed +30 pair, so increase rises to 105.
	res2 := Analyze("requests_total", nil, acceptanceSeries(), AnalysisOptions{From: 0, To: 600, MaxInterval: 600})
	approx(t, "increase without gap rule", res2.Increase, 105)
}

func TestValidateSample(t *testing.T) {
	cases := []struct {
		name string
		s    Sample
		ok   bool
	}{
		{"zero", Sample{T: 0, Value: 0}, true},
		{"normal", Sample{T: 1, Value: 12.5}, true},
		{"negative value rejected", Sample{T: 1, Value: -0.001}, false},
		{"NaN rejected", Sample{T: 1, Value: math.NaN()}, false},
		{"+Inf rejected", Sample{T: 1, Value: math.Inf(1)}, false},
		{"-Inf timestamp rejected", Sample{T: math.Inf(-1), Value: 1}, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			err := ValidateSample(c.s)
			if c.ok && err != nil {
				t.Errorf("unexpected error: %v", err)
			}
			if !c.ok && err == nil {
				t.Errorf("expected rejection for %+v", c.s)
			}
		})
	}
}

// TestConstantCounter: a reset to the same value (delta 0) is not a reset and
// contributes nothing.
func TestConstantCounter(t *testing.T) {
	s := []Sample{{T: 0, Value: 7}, {T: 10, Value: 7}, {T: 20, Value: 7}}
	res := Analyze("c", nil, s, AnalysisOptions{From: 0, To: 20, MaxInterval: 100})
	approx(t, "increase", res.Increase, 0)
	if len(res.Resets) != 0 {
		t.Errorf("flat counter has no resets")
	}
}

// TestLeadingUnobservedRegion: the window starts before any sample; the gap is
// reported, not filled.
func TestLeadingUnobservedRegion(t *testing.T) {
	s := []Sample{{T: 30, Value: 10}, {T: 60, Value: 40}}
	res := Analyze("x", nil, s, AnalysisOptions{From: 0, To: 60, MaxInterval: 1000})
	approx(t, "unobserved_before", res.UnobservedBefore, 30)
	// Only the observed 30s interval counts fully.
	approx(t, "increase", res.Increase, 30)
	approx(t, "coverage", res.Coverage, 0.5)
}

// TestReversedWindow: to < from is treated as a zero-width window and panics
// nothing.
func TestReversedWindow(t *testing.T) {
	s := []Sample{{T: 0, Value: 0}, {T: 10, Value: 5}}
	res := Analyze("x", nil, s, AnalysisOptions{From: 10, To: 0})
	if res.WindowDuration != 0 {
		t.Errorf("reversed window duration = %v, want 0", res.WindowDuration)
	}
	if res.Increase != 0 {
		t.Errorf("reversed window increase = %v, want 0", res.Increase)
	}
}
