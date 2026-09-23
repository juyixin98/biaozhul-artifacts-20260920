package twap

import (
	"math/big"
	"testing"
)

const sec = int64(1_000_000)

func exactNumDen(r *Result) (int64, int64) {
	return r.TWAPNum.Int64(), r.TWAPDen.Int64()
}

func TestUnequalIntervalsIsNotArithmeticMean(t *testing.T) {
	// Window [0, 60s). Samples at t=0 p=100 (10s), t=10s p=200 (50s).
	// Arithmetic mean of samples would be 150. Time-weighted mean is
	// (100*10 + 200*50)/60 = 183.333... = 550/3.
	samples := []Sample{
		{TS: 10 * sec, Price: 200, Source: "s"},
		{TS: 0, Price: 100, Source: "s"},
	}
	r, err := Compute(samples, Params{
		WindowStart: 0, WindowEnd: 60 * sec, StaleAfterMicros: 120 * sec,
	})
	if err != nil {
		t.Fatal(err)
	}
	if r.CoveredMicros != 60*sec {
		t.Fatalf("coverage = %d, want %d", r.CoveredMicros, 60*sec)
	}
	n, d := exactNumDen(r)
	if n != 550 || d != 3 {
		t.Fatalf("twap = %d/%d, want 550/3", n, d)
	}
	f := r.FloatTWAP()
	if f == nil || *f < 183.33 || *f > 183.34 {
		t.Fatalf("float twap = %v, want ~183.333", f)
	}
}

func TestConstantWholeWindow(t *testing.T) {
	// One sample at window start, no changes for the full window.
	samples := []Sample{{TS: 0, Price: 42, Source: "s"}}
	r, err := Compute(samples, Params{
		WindowStart: 0, WindowEnd: 60 * sec, StaleAfterMicros: 120 * sec,
	})
	if err != nil {
		t.Fatal(err)
	}
	n, d := exactNumDen(r)
	if n != 42 || d != 1 {
		t.Fatalf("twap = %d/%d, want 42/1", n, d)
	}
	if r.CoveredMicros != 60*sec || r.FloatCoverage() != 1.0 {
		t.Fatalf("coverage = %d/%v, want full", r.CoveredMicros, r.FloatCoverage())
	}
	if r.ConflictCount != 0 {
		t.Fatalf("conflicts = %d, want 0", r.ConflictCount)
	}
	if len(r.Segments) != 1 {
		t.Fatalf("segments = %d, want 1", len(r.Segments))
	}
}

func TestWindowBoundaryHalfOpen(t *testing.T) {
	// Window [60s,120s). A sample exactly at 60s covers from 60s; a
	// sample exactly at 120s does NOT participate (half-open end).
	samples := []Sample{
		{TS: 60 * sec, Price: 10, Source: "s"},
		{TS: 120 * sec, Price: 99, Source: "s"}, // excluded
	}
	r, err := Compute(samples, Params{
		WindowStart: 60 * sec, WindowEnd: 120 * sec,
		StaleAfterMicros: 120 * sec,
	})
	if err != nil {
		t.Fatal(err)
	}
	n, d := exactNumDen(r)
	if n != 10 || d != 1 {
		t.Fatalf("twap = %d/%d, want 10 (sample at end must not leak in)", n, d)
	}
	if r.SamplesUsed != 1 {
		t.Fatalf("samples used = %d, want 1", r.SamplesUsed)
	}
}

func TestPreWindowAnchorCoversWholeWindow(t *testing.T) {
	// Only sample is at t=-5s with p=7; window [0,60s). Forward-fill:
	// the assertion made at -5s holds into the window, so it IS covered.
	samples := []Sample{{TS: -5 * sec, Price: 7, Source: "s"}}
	r, err := Compute(samples, Params{
		WindowStart: 0, WindowEnd: 60 * sec, StaleAfterMicros: 120 * sec,
	})
	if err != nil {
		t.Fatal(err)
	}
	if !r.HasAnchor {
		t.Fatal("want has_anchor=true")
	}
	if r.CoveredMicros != 60*sec {
		t.Fatalf("coverage = %d, want full via anchor", r.CoveredMicros)
	}
	n, _ := exactNumDen(r)
	if n != 7 {
		t.Fatalf("twap numerator = %d, want 7", n)
	}
}

func TestNoDataZeroCoverage(t *testing.T) {
	r, err := Compute(nil, Params{
		WindowStart: 0, WindowEnd: 60 * sec, StaleAfterMicros: 30 * sec,
	})
	if err != nil {
		t.Fatal(err)
	}
	if r.CoveredMicros != 0 || r.FloatCoverage() != 0 {
		t.Fatalf("coverage = %d, want 0", r.CoveredMicros)
	}
	if r.FloatTWAP() != nil {
		t.Fatalf("twap = %v, want nil for zero coverage", r.FloatTWAP())
	}
	if !r.Stale {
		t.Fatal("no data must be stale")
	}
}

func TestInsufficientCoveragePrefix(t *testing.T) {
	// Window [0,60s), first sample at 20s. The prefix [0,20s) is
	// uncovered (no anchor existed before the window's first sample).
	samples := []Sample{{TS: 20 * sec, Price: 100, Source: "s"}}
	r, err := Compute(samples, Params{
		WindowStart: 0, WindowEnd: 60 * sec, StaleAfterMicros: 120 * sec,
	})
	if err != nil {
		t.Fatal(err)
	}
	if r.CoveredMicros != 40*sec {
		t.Fatalf("covered = %d, want %d", r.CoveredMicros, 40*sec)
	}
	if r.FloatCoverage() < 0.6666 || r.FloatCoverage() > 0.6667 {
		t.Fatalf("coverage = %v, want 2/3", r.FloatCoverage())
	}
	if r.HasAnchor {
		t.Fatal("want has_anchor=false")
	}
	n, d := exactNumDen(r)
	// TWAP is defined over the covered duration only.
	if n != 100 || d != 1 {
		t.Fatalf("twap = %d/%d, want 100/1", n, d)
	}
	if len(r.Segments) != 2 || r.Segments[0].Covered || !r.Segments[1].Covered {
		t.Fatalf("segments = %+v, want [uncovered, covered]", r.Segments)
	}
}

func TestDuplicateTimestampSameSourceLastWins(t *testing.T) {
	// Two rows from the same source at the same timestamp: treated as an
	// upsert, the last value is authoritative, never double-weighted.
	samples := []Sample{
		{TS: 0, Price: 100, Source: "s"},
		{TS: 0, Price: 200, Source: "s"},
	}
	r, err := Compute(samples, Params{
		WindowStart: 0, WindowEnd: 30 * sec, StaleAfterMicros: 60 * sec,
	})
	if err != nil {
		t.Fatal(err)
	}
	n, _ := exactNumDen(r)
	if n != 200 {
		t.Fatalf("twap = %d, want 200 (last duplicate wins)", n)
	}
	if r.SamplesUsed != 1 {
		t.Fatalf("samples used = %d, want 1 deduped row", r.SamplesUsed)
	}
}

func TestSourceConflictDeterministicWinner(t *testing.T) {
	// Two sources disagree for the whole window: lexically smallest
	// source wins, conflict flag is set and reported on the segment.
	samples := []Sample{
		{TS: 0, Price: 100, Source: "exchange-b"},
		{TS: 0, Price: 120, Source: "exchange-a"},
	}
	r, err := Compute(samples, Params{
		WindowStart: 0, WindowEnd: 60 * sec, StaleAfterMicros: 120 * sec,
	})
	if err != nil {
		t.Fatal(err)
	}
	n, _ := exactNumDen(r)
	if n != 120 {
		t.Fatalf("twap = %d, want 120 (exchange-a wins)", n)
	}
	if r.ConflictCount != 1 {
		t.Fatalf("conflicts = %d, want 1", r.ConflictCount)
	}
	if !r.Segments[0].Conflict || r.Segments[0].Sources != 2 {
		t.Fatalf("segment = %+v, want conflict with 2 sources", r.Segments[0])
	}
}

func TestSourceConflictResolvesMidWindow(t *testing.T) {
	// t=0: a=100,b=102 (conflict, a wins 100)
	// t=30s: a=110, b drops off forward-fill? b keeps 102 -> conflict continues
	// t=30s: b=110 as well -> agree at 110
	samples := []Sample{
		{TS: 0, Price: 100, Source: "a"},
		{TS: 0, Price: 102, Source: "b"},
		{TS: 30 * sec, Price: 110, Source: "a"},
		{TS: 30 * sec, Price: 110, Source: "b"},
	}
	r, err := Compute(samples, Params{
		WindowStart: 0, WindowEnd: 60 * sec, StaleAfterMicros: 120 * sec,
	})
	if err != nil {
		t.Fatal(err)
	}
	// (100*30 + 110*30)/60 = 105
	n, d := exactNumDen(r)
	if n != 105 || d != 1 {
		t.Fatalf("twap = %d/%d, want 105", n, d)
	}
	// Conflict present on [0,30s), absent on [30s,60s).
	var conflictDur int64
	for _, seg := range r.Segments {
		if seg.Conflict {
			conflictDur += seg.End - seg.Start
		}
	}
	if conflictDur != 30*sec {
		t.Fatalf("conflict duration = %d, want 30s", conflictDur)
	}
}

func TestFuturePricesNeverFillHistory(t *testing.T) {
	// now = 10s: the sample at 30s is a future price and must not be
	// used. With a sample at 0s p=100, the window [0,60s) is covered
	// only until now; [now,60s) stays uncovered and TWAP is null-safe.
	samples := []Sample{
		{TS: 0, Price: 100, Source: "s"},
		{TS: 30 * sec, Price: 500, Source: "s"}, // future
	}
	r, err := Compute(samples, Params{
		WindowStart: 0, WindowEnd: 60 * sec,
		Now: 10 * sec, StaleAfterMicros: 120 * sec,
	})
	if err != nil {
		t.Fatal(err)
	}
	if r.CoveredMicros != 10*sec {
		t.Fatalf("covered = %d, want 10s (future excluded)", r.CoveredMicros)
	}
	for _, seg := range r.Segments {
		if seg.Covered && seg.Price == 500 {
			t.Fatalf("future price 500 leaked into segment %+v", seg)
		}
	}
}

func TestStaleFlag(t *testing.T) {
	// Sample at t=0; evaluated at 60s (effective end), stale-after 30s.
	samples := []Sample{{TS: 0, Price: 100, Source: "s"}}
	r, err := Compute(samples, Params{
		WindowStart: 0, WindowEnd: 60 * sec,
		Now: 60 * sec, StaleAfterMicros: 30 * sec,
	})
	if err != nil {
		t.Fatal(err)
	}
	if !r.Stale {
		t.Fatal("age 60s > 30s must be stale")
	}

	r2, err := Compute(samples, Params{
		WindowStart: 0, WindowEnd: 60 * sec,
		Now: 60 * sec, StaleAfterMicros: 90 * sec,
	})
	if err != nil {
		t.Fatal(err)
	}
	if r2.Stale {
		t.Fatal("age 60s <= 90s must not be stale")
	}
}

func TestExactRationalIsReduced(t *testing.T) {
	// p=3 for 20s, p=6 for 40s over 60s:
	// integral = 60+240 = 300, /60 = 5 exactly.
	samples := []Sample{
		{TS: 0, Price: 3, Source: "s"},
		{TS: 20 * sec, Price: 6, Source: "s"},
	}
	r, err := Compute(samples, Params{
		WindowStart: 0, WindowEnd: 60 * sec, StaleAfterMicros: 120 * sec,
	})
	if err != nil {
		t.Fatal(err)
	}
	n, d := exactNumDen(r)
	if n != 5 || d != 1 {
		t.Fatalf("twap = %d/%d, want 5/1 reduced", n, d)
	}
	// WeightedSum retains the true integral even though TWAP reduced.
	if r.WeightedSum.Cmp(big.NewInt(300*sec)) != 0 {
		t.Fatalf("weighted sum = %v, want %d", r.WeightedSum, 300*sec)
	}
}

func TestRejectsBadWindow(t *testing.T) {
	if _, err := Compute(nil, Params{WindowStart: 10, WindowEnd: 10}); err == nil {
		t.Fatal("want error for zero-length window")
	}
	if _, err := Compute(nil, Params{WindowStart: 10, WindowEnd: 5}); err == nil {
		t.Fatal("want error for inverted window")
	}
}
