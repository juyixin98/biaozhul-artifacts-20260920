package scaler

import (
	"errors"
	"math"
	"testing"
)

func ptr(x float64) *float64 { return &x }

func baseCfg() Config {
	return Config{MinReplicas: 1, MaxReplicas: 10, TargetPct: 50, TolerancePct: 10, StableWindowSec: 60}
}

func pods(n int, util float64) []PodSample {
	out := make([]PodSample, n)
	for i := range out {
		out[i] = PodSample{Name: "p" + string(rune('a'+i)), Ready: true, UtilizationPct: ptr(util)}
	}
	return out
}

func hasReason(d Decision, code string) bool {
	for _, r := range d.Reasons {
		if r == code {
			return true
		}
	}
	return false
}

// The core HPA formula: desiredReplicas = ceil[currentReplicas * (avgUtil/target)].
func TestFormulaBasicScaleUp(t *testing.T) {
	// 4 ready pods all at 90% against a 50% target -> ratio 1.8 -> 7.2 -> 8.
	d, err := Calculate(baseCfg(), Snapshot{CurrentReplicas: 4, Pods: pods(4, 90)}, 1000, nil)
	if err != nil {
		t.Fatal(err)
	}
	if d.RawProposed != 8 || d.Final != 8 || d.Action != "scaleup" {
		t.Fatalf("got raw=%d final=%d action=%s, want 8/8/scaleup", d.RawProposed, d.Final, d.Action)
	}
	if math.Abs(d.AvgUtilizationPct-90) > 1e-9 || math.Abs(d.Ratio-1.8) > 1e-9 {
		t.Fatalf("avg=%v ratio=%v", d.AvgUtilizationPct, d.Ratio)
	}
	if !hasReason(d, ReasonScaleUpImmediate) || !hasReason(d, ReasonRawCeil) {
		t.Fatalf("reasons=%v", d.Reasons)
	}
}

func TestToleranceBandHoldsAndRecordsNoProposal(t *testing.T) {
	// ratio 1.05 is inside 1±0.10 -> hold, raw must be absent (-1).
	d, err := Calculate(baseCfg(), Snapshot{CurrentReplicas: 4, Pods: pods(4, 52.5)}, 1000, nil)
	if err != nil {
		t.Fatal(err)
	}
	if d.Action != "hold" || d.Final != 4 || d.RawProposed != -1 {
		t.Fatalf("got action=%s final=%d raw=%d", d.Action, d.Final, d.RawProposed)
	}
	if !hasReason(d, ReasonWithinTolerance) {
		t.Fatalf("reasons=%v", d.Reasons)
	}
}

func TestScaleDownUsesMaxAcrossStableWindow(t *testing.T) {
	cfg := baseCfg()
	now := int64(60_000)
	// Older proposals inside the 60s window: 8 (a previous scale-up), then 3.
	hist := []WindowEntry{
		{MetricTimeMs: 0, RawProposed: 8},
		{MetricTimeMs: 30_000, RawProposed: 3},
	}
	// Current raw proposal is 3 (low utilization), but the window max is 8:
	// scale-down must NOT happen yet.
	d, err := Calculate(cfg, Snapshot{CurrentReplicas: 8, Pods: pods(8, 18.75)}, now, hist)
	if err != nil {
		t.Fatal(err)
	}
	if d.RawProposed != 3 {
		t.Fatalf("raw=%d want 3", d.RawProposed)
	}
	if d.Final != 8 || d.Action != "hold" {
		t.Fatalf("final=%d action=%s, want 8/hold (stable window)", d.Final, d.Action)
	}
	if !hasReason(d, ReasonStableWindow) || len(d.WindowUsed) != 2 {
		t.Fatalf("reasons=%v window=%v", d.Reasons, d.WindowUsed)
	}

	// After the proposal of 8 ages out of the window, max becomes 3 -> scale down.
	later := int64(61_000)
	hist2 := []WindowEntry{{MetricTimeMs: 30_000, RawProposed: 3}}
	d2, err := Calculate(cfg, Snapshot{CurrentReplicas: 8, Pods: pods(8, 18.75)}, later, hist2)
	if err != nil {
		t.Fatal(err)
	}
	if d2.Final != 3 || d2.Action != "scaledown" {
		t.Fatalf("final=%d action=%s, want 3/scaledown", d2.Final, d2.Action)
	}
}

func TestScaleUpIgnoresStableWindow(t *testing.T) {
	cfg := baseCfg()
	hist := []WindowEntry{{MetricTimeMs: 0, RawProposed: 2}}
	d, err := Calculate(cfg, Snapshot{CurrentReplicas: 4, Pods: pods(4, 95)}, 10_000, hist)
	if err != nil {
		t.Fatal(err)
	}
	if d.Final != 8 || d.Action != "scaleup" {
		t.Fatalf("final=%d action=%s, scale-up must be immediate", d.Final, d.Action)
	}
	if hasReason(d, ReasonStableWindow) {
		t.Fatalf("scale-up must not consult the window: %v", d.Reasons)
	}
}

func TestAllMetricsMissingHoldsWithoutZeroFill(t *testing.T) {
	// Every ready pod reports nil; average must be undefined (0 + flag false),
	// never computed as zero, and the recommendation holds current replicas.
	snap := Snapshot{CurrentReplicas: 5, Pods: []PodSample{
		{Name: "a", Ready: true, UtilizationPct: nil},
		{Name: "b", Ready: true, UtilizationPct: nil},
	}}
	d, err := Calculate(baseCfg(), snap, 1000, nil)
	if err != nil {
		t.Fatal(err)
	}
	if d.MetricPresent || d.AvgUtilizationPct != 0 || d.Ratio != 0 || d.RawProposed != -1 {
		t.Fatalf("metricPresent=%v avg=%v ratio=%v raw=%d", d.MetricPresent, d.AvgUtilizationPct, d.Ratio, d.RawProposed)
	}
	if d.Final != 5 || d.Action != "hold" || !hasReason(d, ReasonAllMetricsMissing) {
		t.Fatalf("final=%d action=%s reasons=%v", d.Final, d.Action, d.Reasons)
	}
}

func TestPartialMetricsAverageOverReportingOnly(t *testing.T) {
	// 3 ready pods: 80, 60 and a missing one. Average is over the 2 reporting
	// pods only (70), missing is NOT zero (which would give 46.67).
	snap := Snapshot{CurrentReplicas: 3, Pods: []PodSample{
		{Name: "a", Ready: true, UtilizationPct: ptr(80)},
		{Name: "b", Ready: true, UtilizationPct: ptr(60)},
		{Name: "c", Ready: true, UtilizationPct: nil},
	}}
	d, err := Calculate(baseCfg(), snap, 1000, nil)
	if err != nil {
		t.Fatal(err)
	}
	if math.Abs(d.AvgUtilizationPct-70) > 1e-9 {
		t.Fatalf("avg=%v want 70 (missing must not be zero-filled)", d.AvgUtilizationPct)
	}
	if d.ReadyMissing != 1 || !hasReason(d, ReasonPartialMetrics) {
		t.Fatalf("readyMissing=%d reasons=%v", d.ReadyMissing, d.Reasons)
	}
}

func TestUnreadyPodsExcludedFromAverage(t *testing.T) {
	// 3 ready at 80% + 2 unready (even if they carry a 0 value, it is ignored).
	snap := Snapshot{CurrentReplicas: 5, Pods: []PodSample{
		{Name: "a", Ready: true, UtilizationPct: ptr(80)},
		{Name: "b", Ready: true, UtilizationPct: ptr(80)},
		{Name: "c", Ready: true, UtilizationPct: ptr(80)},
		{Name: "d", Ready: false, UtilizationPct: ptr(0)},
		{Name: "e", Ready: false, UtilizationPct: nil},
	}}
	d, err := Calculate(baseCfg(), snap, 1000, nil)
	if err != nil {
		t.Fatal(err)
	}
	if d.ReadyCount != 3 || d.UnreadyCount != 2 || d.ReadyReporting != 3 {
		t.Fatalf("counts ready=%d unready=%d reporting=%d", d.ReadyCount, d.UnreadyCount, d.ReadyReporting)
	}
	// 80% over 3 ready pods: avg 80 (a 0-filled average over all 5 would be 48).
	if math.Abs(d.AvgUtilizationPct-80) > 1e-9 {
		t.Fatalf("avg=%v want 80", d.AvgUtilizationPct)
	}
	if !hasReason(d, ReasonUnreadyExcluded) {
		t.Fatalf("reasons=%v", d.Reasons)
	}
}

func TestZeroTargetRejectedNotInfinity(t *testing.T) {
	cfg := baseCfg()
	cfg.TargetPct = 0
	_, err := Calculate(cfg, Snapshot{CurrentReplicas: 3, Pods: pods(3, 50)}, 1000, nil)
	if !errors.Is(err, ErrZeroTarget) {
		t.Fatalf("got %v, want ErrZeroTarget", err)
	}
}

func TestMaxReplicasCap(t *testing.T) {
	cfg := baseCfg() // max 10
	d, err := Calculate(cfg, Snapshot{CurrentReplicas: 10, Pods: pods(10, 200)}, 1000, nil)
	if err != nil {
		t.Fatal(err)
	}
	// ratio 4.0 -> raw 40, capped to 10.
	if d.RawProposed != 40 || d.Final != 10 {
		t.Fatalf("raw=%d final=%d, want raw 40 capped to 10", d.RawProposed, d.Final)
	}
	if !hasReason(d, ReasonCappedMax) {
		t.Fatalf("reasons=%v", d.Reasons)
	}
}

func TestMinReplicasClamp(t *testing.T) {
	cfg := baseCfg()
	cfg.MinReplicas = 2
	hist := []WindowEntry{{MetricTimeMs: 0, RawProposed: 1}}
	d, err := Calculate(cfg, Snapshot{CurrentReplicas: 4, Pods: pods(4, 5)}, 1000, hist)
	if err != nil {
		t.Fatal(err)
	}
	if d.Final != 2 || !hasReason(d, ReasonCappedMin) {
		t.Fatalf("final=%d reasons=%v, want clamp to 2", d.Final, d.Reasons)
	}
}

func TestEmptyWindowOnScaleDownHolds(t *testing.T) {
	// First ever low-utilization sample: no history yet -> conservative hold,
	// never a blind scale-down.
	d, err := Calculate(baseCfg(), Snapshot{CurrentReplicas: 8, Pods: pods(8, 10)}, 1000, nil)
	if err != nil {
		t.Fatal(err)
	}
	if d.Final != 8 || d.Action != "hold" || !hasReason(d, ReasonWindowNoProposal) {
		t.Fatalf("final=%d action=%s reasons=%v", d.Final, d.Action, d.Reasons)
	}
}

// --- scenario simulations driven by a manually advanced clock -------------

// simEngine replays demand against Calculate the way the service does: it
// stores raw out-of-band proposals keyed by metric time and feeds back the
// window contents each tick.
type simEngine struct {
	cfg     Config
	current int
	hist    []WindowEntry
}

// demand is total utilization*replicas; per-pod util = demand/current.
func (s *simEngine) tick(nowMs int64, demand float64, missing bool) Decision {
	var ps []PodSample
	if missing {
		ps = []PodSample{{Name: "only", Ready: true, UtilizationPct: nil}}
	} else {
		util := demand / float64(s.current)
		ps = pods(s.current, util)
	}
	d, err := Calculate(s.cfg, Snapshot{CurrentReplicas: s.current, Pods: ps}, nowMs, s.hist)
	if err != nil {
		panic(err)
	}
	if d.RawProposed >= 0 {
		s.hist = append(s.hist, WindowEntry{MetricTimeMs: nowMs, RawProposed: d.RawProposed})
	}
	s.current = d.Final
	return d
}

// TestStepLoadScenario: steady low -> load doubles -> immediate scale up ->
// load drops -> scale-down blocked for the full stable window, then allowed.
func TestStepLoadScenario(t *testing.T) {
	s := &simEngine{cfg: baseCfg(), current: 3}
	// 3 pods at target (150/3 = 50%): within tolerance, no proposal.
	d := s.tick(0, 150, false)
	if d.Action != "hold" || d.Final != 3 {
		t.Fatalf("steady state: %v", d)
	}

	// Step up to demand 400: desired ~8, scale-up immediate.
	d = s.tick(10_000, 400, false)
	if d.Action != "scaleup" || d.Final != 8 {
		t.Fatalf("step up: action=%s final=%d", d.Action, d.Final)
	}

	// At 8 pods demand 400 -> 50% again: settle, no new proposal.
	d = s.tick(20_000, 400, false)
	if d.Action != "hold" || d.Final != 8 {
		t.Fatalf("settle high: action=%s final=%d", d.Action, d.Final)
	}

	// Step down to demand 150 at t=30s: raw 3 but window still holds the
	// proposal of 8 from t=10s -> must hold at 8 (no thrash).
	d = s.tick(30_000, 150, false)
	if d.RawProposed != 3 || d.Final != 8 || d.Action != "hold" {
		t.Fatalf("first low tick: raw=%d final=%d action=%s", d.RawProposed, d.Final, d.Action)
	}

	// Keep low; window keeps holding 8 while the t=10s proposal is in range.
	d = s.tick(60_000, 150, false) // cutoff 0: t=10s proposal still in
	if d.Final != 8 {
		t.Fatalf("at 60s window boundary still holds 8, got %d", d.Final)
	}
	d = s.tick(70_000, 150, false) // cutoff 10s: window is left-closed, t=10s still in
	if d.Final != 8 {
		t.Fatalf("at 70s the inclusive window edge still holds 8, got %d", d.Final)
	}
	d = s.tick(80_000, 150, false) // cutoff 20s: the proposal of 8 finally aged out
	if d.Final != 3 || d.Action != "scaledown" {
		t.Fatalf("after window: final=%d action=%s, want 3/scaledown", d.Final, d.Action)
	}
}

// TestPeriodicLoadScenario: demand oscillates; scale-ups are immediate but
// scale-downs are held to the window maximum, so replica count never flaps
// downward faster than the stable window allows.
func TestPeriodicLoadScenario(t *testing.T) {
	s := &simEngine{cfg: baseCfg(), current: 3}
	var finals []int
	for ts := int64(0); ts <= 240_000; ts += 10_000 {
		// Period 60s: high for the first 30s, low for the next 30s.
		high := (ts/30_000)%2 == 0
		demand := 150.0
		if high {
			demand = 400
		}
		d := s.tick(ts, demand, false)
		finals = append(finals, d.Final)
		// Invariant: a scale-down recommendation can never be lower than the
		// maximum raw proposal observed in the preceding stable window.
		if d.Action == "scaledown" {
			for _, e := range d.WindowUsed {
				if e.RawProposed > d.Final {
					t.Fatalf("t=%d scaledown to %d but window contains %d", ts, d.Final, e.RawProposed)
				}
			}
		}
	}
	// The peak must have been reached immediately; the trough must trail by at
	// least the stable window rather than following every oscillation.
	peak, trough := finals[0], finals[0]
	for _, v := range finals {
		if v > peak {
			peak = v
		}
		if v < trough {
			trough = v
		}
	}
	if peak != 8 {
		t.Fatalf("peak=%d want 8", peak)
	}
	if trough < 3 {
		t.Fatalf("trough=%d below min expected 3", trough)
	}
}

// TestLongMissingPeriod: a long gap with NO metrics must hold replicas and add
// no proposals; when metrics resume, the (now aged-out, empty) window yields a
// conservative hold, and only the next sample can scale down.
func TestLongMissingPeriod(t *testing.T) {
	s := &simEngine{cfg: baseCfg(), current: 8}
	// One historical scale-down proposal of 3.
	s.hist = []WindowEntry{{MetricTimeMs: 0, RawProposed: 3}}

	// 90s of missing metrics, well past the 60s window: every tick holds at 8.
	for ts := int64(10_000); ts <= 90_000; ts += 10_000 {
		d := s.tick(ts, 0, true)
		if d.Final != 8 || d.Action != "hold" || !hasReason(d, ReasonAllMetricsMissing) {
			t.Fatalf("missing t=%d: final=%d action=%s", ts, d.Final, d.Action)
		}
		if d.RawProposed != -1 {
			t.Fatalf("missing sample must not produce a raw proposal, got %d", d.RawProposed)
		}
	}

	// Metrics resume low. No proposal survives within the window, so even
	// though the raw value is 3, the engine conservatively holds once.
	d := s.tick(100_000, 150, false)
	if d.RawProposed != 3 || d.Final != 8 || !hasReason(d, ReasonWindowNoProposal) {
		t.Fatalf("resume: raw=%d final=%d reasons=%v", d.RawProposed, d.Final, d.Reasons)
	}
	// Subsequent sample: its predecessor (3) is now in the window -> scale down.
	d = s.tick(110_000, 150, false)
	if d.Final != 3 || d.Action != "scaledown" {
		t.Fatalf("second resume tick: final=%d action=%s", d.Final, d.Action)
	}
}
