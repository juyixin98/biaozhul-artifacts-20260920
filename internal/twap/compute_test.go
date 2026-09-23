package twap_test

import (
	"math/big"
	"testing"

	"twap-service/internal/domain"
	"twap-service/internal/twap"
)

// sampleTS is shorthand: seconds -> microseconds.
func at(sec int64) int64 { return sec * domain.MicroPerSec }

func eff(tsUs, price int64, src string) domain.EffectiveSample {
	return domain.EffectiveSample{TS: tsUs, Price: price, Source: src}
}

func bigI(x int64) *big.Int { return big.NewInt(x) }

func TestUnequalIntervalsIsWeightedNotMean(t *testing.T) {
	// Window [0,60). Prices 100 held 50s, 200 held 10s.
	// Arithmetic mean of the two samples is 150; the TIME-WEIGHTED mean is
	// (100*50 + 200*10)/60 = 116.666667.
	events := []domain.EffectiveSample{
		eff(at(0), 100, "A"),
		eff(at(50), 200, "B"),
	}
	res := twap.ComputeWindow("X", 0, 60, events, 60, 120)

	if res.TWAPString != "116.666667" {
		t.Fatalf("TWAP = %s, want 116.666667 (must be time-weighted, not mean 150)",
			res.TWAPString)
	}
	if res.CoverageString != "1.000000" {
		t.Fatalf("coverage = %s want 1.000000", res.CoverageString)
	}
	if res.Integral.Cmp(bigI(int64(100*50+200*10)*domain.MicroPerSec)) != 0 {
		t.Fatalf("integral = %s", res.Integral.String())
	}
	if res.Stale {
		t.Fatalf("fully covered fresh window must not be stale")
	}
}

func TestCarryInBeforeWindowStart(t *testing.T) {
	// Sample at -10 with price 100 carries into [0,60); next event at 30.
	events := []domain.EffectiveSample{
		eff(at(-10), 100, "A"),
		eff(at(30), 300, "B"),
	}
	res := twap.ComputeWindow("X", 0, 60, events, 60, 120)
	// 100 for 30s + 300 for 30s = 200.
	if res.TWAPString != "200.000000" {
		t.Fatalf("TWAP = %s want 200.000000", res.TWAPString)
	}
}

func TestNoSampleBeforeStartIsUncoveredNotBackfilled(t *testing.T) {
	// First observation at t=20. [0,20) must stay uncovered even though a
	// future price exists — future prices never fill history.
	events := []domain.EffectiveSample{eff(at(20), 100, "A")}
	res := twap.ComputeWindow("X", 0, 60, events, 60, 120)
	if res.CoveredUsec != 40*domain.MicroPerSec {
		t.Fatalf("covered = %d want 40s", res.CoveredUsec/domain.MicroPerSec)
	}
	if res.TWAPString != "100.000000" {
		t.Fatalf("TWAP = %s want 100.000000", res.TWAPString)
	}
	if !res.Stale {
		t.Fatalf("leading gap must mark the window stale")
	}
}

func TestNoSamplesAtAll(t *testing.T) {
	res := twap.ComputeWindow("X", 0, 60, nil, 60, 120)
	if res.CoveredUsec != 0 || res.TWAPString != "" || !res.Stale {
		t.Fatalf("empty window: %+v", res)
	}
	if res.CoverageString != "0.000000" {
		t.Fatalf("coverage = %s", res.CoverageString)
	}
}

func TestConstantPriceWholeWindow(t *testing.T) {
	// "整窗无变化": a single sample at the start carried the whole window.
	events := []domain.EffectiveSample{eff(at(0), 42, "A")}
	res := twap.ComputeWindow("X", 0, 60, events, 60, 120)
	if res.TWAPString != "42.000000" || res.CoverageString != "1.000000" || res.Stale {
		t.Fatalf("constant window wrong: %+v", res)
	}
}

func TestWindowBoundarySamples(t *testing.T) {
	// A sample exactly at the END of [0,60) belongs to the next window, not
	// this one; a sample exactly at START belongs to us.
	events := []domain.EffectiveSample{
		eff(at(0), 100, "A"),
		eff(at(60), 999, "B"), // end boundary: excluded here
	}
	res := twap.ComputeWindow("X", 0, 60, events, 120, 120)
	if res.TWAPString != "100.000000" {
		t.Fatalf("end-boundary sample leaked into window: %s", res.TWAPString)
	}
	// Next window starts with the boundary sample in effect at its open.
	res2 := twap.ComputeWindow("X", 60, 120, events, 120, 120)
	if res2.TWAPString != "999.000000" {
		t.Fatalf("start-boundary sample must belong to next window: %s", res2.TWAPString)
	}
}

func TestStaleHorizonCutsCarry(t *testing.T) {
	// One sample at t=0, horizon 30s: coverage ends at t=30, window is stale
	// with coverage 0.5.
	events := []domain.EffectiveSample{eff(at(0), 100, "A")}
	res := twap.ComputeWindow("X", 0, 60, events, 60, 30)
	if res.CoveredUsec != 30*domain.MicroPerSec {
		t.Fatalf("covered = %d want 30s", res.CoveredUsec/domain.MicroPerSec)
	}
	if res.CoverageString != "0.500000" {
		t.Fatalf("coverage = %s want 0.500000", res.CoverageString)
	}
	if !res.Stale {
		t.Fatal("must be stale after horizon")
	}
}

func TestAsOfCapsFutureWindows(t *testing.T) {
	// Current window at asOf=20: only 20 seconds observable.
	events := []domain.EffectiveSample{eff(at(0), 100, "A")}
	res := twap.ComputeWindow("X", 0, 60, events, 20, 120)
	if res.CoveredUsec != 20*domain.MicroPerSec {
		t.Fatalf("covered = %d want 20s", res.CoveredUsec/domain.MicroPerSec)
	}
	if !res.Stale {
		t.Fatal("in-flight window must be stale")
	}
	// Future samples (past asOf) are invisible.
	future := []domain.EffectiveSample{eff(at(10), 100, "A"), eff(at(25), 500, "F")}
	res2 := twap.ComputeWindow("X", 0, 60, future, 20, 120)
	if res2.TWAPString != "100.000000" {
		t.Fatalf("future sample filled history: %s", res2.TWAPString)
	}
}

func TestDuplicateTimestampConflictResolution(t *testing.T) {
	// Same ts, three sources, different prices. Priority B=10 beats A=0 and
	// C=0; among equal priorities the greatest name wins (C > A).
	samples := []domain.Sample{
		{Symbol: "X", TS: at(0), Price: 100, Source: "A"},
		{Symbol: "X", TS: at(0), Price: 200, Source: "C"},
		{Symbol: "X", TS: at(0), Price: 300, Source: "B"},
	}
	got := twap.ResolveEffective(samples, map[string]int{"A": 0, "B": 10, "C": 0})
	if len(got) != 1 {
		t.Fatalf("want one effective event, got %d", len(got))
	}
	if got[0].Price != 300 || got[0].Source != "B" {
		t.Fatalf("winner = %s/%d want B/300", got[0].Source, got[0].Price)
	}
	if len(got[0].Conflicting) != 2 {
		t.Fatalf("conflicts = %v want [A C]", got[0].Conflicting)
	}

	// Equal-price duplicate is NOT a conflict.
	same := []domain.Sample{
		{Symbol: "X", TS: at(0), Price: 100, Source: "A"},
		{Symbol: "X", TS: at(0), Price: 100, Source: "B"},
	}
	got2 := twap.ResolveEffective(same, map[string]int{})
	if got2[0].Price != 100 || len(got2[0].Conflicting) != 0 {
		t.Fatalf("equal-price duplicate wrongly flagged: %+v", got2[0])
	}
}

func TestMicrosecondWeighting(t *testing.T) {
	// Unequal interval at sub-second granularity: 100 for 0.5s then 200 for
	// 0.5s in a 1s window.
	events := []domain.EffectiveSample{
		{TS: 0, Price: 100, Source: "A"},
		{TS: 500_000, Price: 200, Source: "B"},
	}
	res := twap.ComputeWindow("X", 0, 1, events, 1, 120)
	if res.TWAPString != "150.000000" {
		t.Fatalf("TWAP = %s want 150.000000", res.TWAPString)
	}
}

func TestRoundingHalfAwayFromZero(t *testing.T) {
	cases := []struct {
		num, den int64
		want     string
	}{
		{1, 3, "0.333333"},
		{2, 3, "0.666667"},
		{5, 2, "2.500000"},
		{-2, 3, "-0.666667"},
		{1, 8, "0.125000"},
		{7, 6, "1.166667"},
	}
	for _, c := range cases {
		// ratToFixed is exercised through TWAPString; test via 1s window with
		// synthetic integral: easier to unit-test the decimal helper directly.
		got := twap.TESTRatToFixed(bigI(c.num), bigI(c.den), 6)
		if got != c.want {
			t.Errorf("%d/%d = %s want %s", c.num, c.den, got, c.want)
		}
	}
}

func TestAffectedWindowsRanges(t *testing.T) {
	const w, hz, asOf = int64(60), int64(120), int64(600)

	// Brand-new sample at t=100, next event at t=110: only window [60,120).
	got := twap.AffectedWindows(at(100), at(50), at(110), false, w, hz, asOf)
	if len(got) != 1 || got[0] != at(60) {
		t.Fatalf("new sample: %v", got)
	}
	// Correction at t=100 (prev at 50): backward to max(50, -20)=50, forward
	// to next 110 -> windows [0,60) and [60,120).
	got = twap.AffectedWindows(at(100), at(50), at(110), true, w, hz, asOf)
	if len(got) != 2 || got[0] != 0 || got[1] != at(60) {
		t.Fatalf("correction: %v", got)
	}
	// No next event, carry reaches horizon at t=220 -> windows containing
	// [100,220): [60,120),[120,180),[180,240).
	got = twap.AffectedWindows(at(100), at(50), 0, false, w, hz, asOf)
	if len(got) != 3 {
		t.Fatalf("horizon carry: %v", got)
	}
	// Sample exactly on a window boundary t=120 affects [120,180) onward.
	got = twap.AffectedWindows(at(120), at(60), 0, false, w, hz, asOf)
	if got[0] != at(120) {
		t.Fatalf("boundary sample: %v", got)
	}
}

func TestCanonicalHashStableAndSensitive(t *testing.T) {
	events := []domain.EffectiveSample{eff(at(0), 100, "A"), eff(at(30), 200, "B")}
	res := twap.ComputeWindow("X", 0, 60, events, 60, 120)
	h1 := twap.ResultHash(res, events, 120)
	h2 := twap.ResultHash(res, events, 120)
	if h1 != h2 {
		t.Fatal("hash must be deterministic")
	}
	// A changed input price must change the hash.
	events2 := []domain.EffectiveSample{eff(at(0), 100, "A"), eff(at(30), 201, "B")}
	res2 := twap.ComputeWindow("X", 0, 60, events2, 60, 120)
	h3 := twap.ResultHash(res2, events2, 120)
	if h1 == h3 {
		t.Fatal("different content produced identical hash")
	}
}
