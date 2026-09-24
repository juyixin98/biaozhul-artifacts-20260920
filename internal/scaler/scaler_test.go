package scaler

import (
	"strings"
	"testing"
	"time"
)

var t0 = time.Date(2026, 9, 24, 12, 0, 0, 0, time.UTC)

// testCfg returns a config with cold-start disabled unless a test overrides it.
func testCfg() Config {
	return Config{
		MinReplicas:      1,
		MaxReplicas:      10,
		TargetPerReplica: 50,
		Tolerance:        0.1,

		MetricsWindowSeconds: 60,
		ColdStartSeconds:     0,

		ScaleUpStabilizationSeconds:   0,
		ScaleDownStabilizationSeconds: 300,

		ScaleUpCooldownSeconds:   60,
		ScaleDownCooldownSeconds: 120,

		ScaleUpMaxStep:   2,
		ScaleDownMaxStep: 1,
	}
}

// feed ingests one sample per pod at time t with the given per-pod value.
// Pods are considered started long ago (no cold start).
func feed(s *Scaler, pods []string, t time.Time, value float64) {
	samples := make([]Sample, 0, len(pods))
	for _, p := range pods {
		samples = append(samples, Sample{
			Time:     t,
			Pod:      p,
			PodStart: t.Add(-time.Hour),
			Value:    value,
		})
	}
	s.Ingest(samples)
}

func pods(n int) []string {
	out := make([]string, n)
	for i := range out {
		out[i] = string(rune('a'+i/26)) + string(rune('a'+i%26))
	}
	return out
}

func hasReason(d Decision, substr string) bool {
	for _, r := range d.Reasons {
		if strings.Contains(r, substr) {
			return true
		}
	}
	return false
}

// Spike: a sudden 5x load jump must be rate-limited per action and then
// throttled by the scale-up cooldown.
func TestSpikeRateLimitAndCooldown(t *testing.T) {
	s := New(testCfg(), 2)
	feed(s, pods(2), t0, 250) // 250/50 = ratio 5 -> raw 10

	d := s.Evaluate(t0)
	if d.Action != ActionScaleUp || d.DesiredReplicas != 4 {
		t.Fatalf("spike: got %s to %d, want scale-up to 4 (rate-limited +2)", d.Action, d.DesiredReplicas)
	}
	if d.RawDesired != 10 {
		t.Fatalf("raw desired = %d, want 10", d.RawDesired)
	}
	if !hasReason(d, "rate limit") {
		t.Fatalf("expected rate-limit reason, got %v", d.Reasons)
	}

	// 30s later, still spiking: cooldown must block another scale-up.
	feed(s, pods(4), t0.Add(30*time.Second), 250)
	d = s.Evaluate(t0.Add(30 * time.Second))
	if d.Action != ActionHold || d.DesiredReplicas != 4 {
		t.Fatalf("cooldown: got %s to %d, want hold at 4", d.Action, d.DesiredReplicas)
	}
	if !hasReason(d, "cooldown") {
		t.Fatalf("expected cooldown reason, got %v", d.Reasons)
	}

	// After the cooldown expires, scaling resumes, again capped to +2.
	feed(s, pods(4), t0.Add(61*time.Second), 250)
	d = s.Evaluate(t0.Add(61 * time.Second))
	if d.Action != ActionScaleUp || d.DesiredReplicas != 6 {
		t.Fatalf("after cooldown: got %s to %d, want scale-up to 6", d.Action, d.DesiredReplicas)
	}
}

// Sustained growth: steadily rising load must produce a staircase of scale-ups.
func TestSustainedGrowth(t *testing.T) {
	s := New(testCfg(), 2)
	want := []int{4, 6, 8, 10} // +2 per action (rate limit), tracking growth
	at := t0
	for i, w := range want {
		load := 50 * float64(s.State().Replicas) * 1.4 // 40% above capacity each round
		n := s.State().Replicas
		feed(s, pods(n), at, load)
		d := s.Evaluate(at)
		if d.Action != ActionScaleUp {
			t.Fatalf("round %d: got %s, want scale-up (reasons: %v)", i, d.Action, d.Reasons)
		}
		if d.DesiredReplicas != w {
			t.Fatalf("round %d: got %d replicas, want %d", i, d.DesiredReplicas, w)
		}
		at = at.Add(61 * time.Second) // past the scale-up cooldown
	}
}

// Oscillation: alternating high/low load must not flap. The tolerance band
// absorbs small wiggles and the scale-down stabilization window blocks
// premature scale-downs.
func TestOscillationNoFlapping(t *testing.T) {
	s := New(testCfg(), 2)
	at := t0
	ups, downs := 0, 0
	// 10 cycles of high (total 180) then low (total 80) load, 61s apart.
	// Per-pod value dilutes as replicas are added, like real traffic.
	for i := 0; i < 10; i++ {
		n := s.State().Replicas
		feed(s, pods(n), at, 180/float64(n))
		d := s.Evaluate(at)
		if d.Action == ActionScaleUp {
			ups++
		}
		at = at.Add(61 * time.Second)

		n = s.State().Replicas
		feed(s, pods(n), at, 80/float64(n))
		d = s.Evaluate(at)
		if d.Action == ActionScaleDown {
			downs++
		}
		at = at.Add(61 * time.Second)
	}
	if ups > 1 {
		t.Fatalf("oscillation caused %d scale-ups, want <= 1", ups)
	}
	if downs != 0 {
		t.Fatalf("oscillation caused %d scale-downs, want 0 (stabilization window should protect)", downs)
	}
}

// Missing metrics must be excluded from the average, never counted as zero.
func TestMissingMetricsNotZero(t *testing.T) {
	s := New(testCfg(), 3)
	feed(s, pods(3), t0, 50) // exactly at target
	d := s.Evaluate(t0)
	if d.Action != ActionHold {
		t.Fatalf("at target: got %s, want hold", d.Action)
	}

	// One pod goes silent; the other two keep reporting 75 (ratio 1.5 over
	// the reporting pods). If the silent pod were counted as zero the ratio
	// would be 1.0 and nothing would happen. 70s later the silent pod's
	// last sample is stale (metrics window = 60s).
	feed(s, pods(2), t0.Add(70*time.Second), 75)
	d = s.Evaluate(t0.Add(70 * time.Second))
	if d.UsageRatio != 1.5 {
		t.Fatalf("usage ratio = %v, want 1.5 (missing pod excluded, not zero)", d.UsageRatio)
	}
	if d.Action != ActionScaleUp {
		t.Fatalf("got %s, want scale-up (missing pod must not drag the average to zero)", d.Action)
	}
	found := false
	for _, e := range d.Excluded {
		if strings.Contains(e.Reason, "missing or stale") {
			found = true
		}
	}
	if !found {
		t.Fatalf("expected exclusion reason for the silent pod, got %+v", d.Excluded)
	}

	// All metrics gone: skip, hold current replicas — never scale to zero.
	d = s.Evaluate(t0.Add(10 * time.Minute))
	if d.Action != ActionSkip {
		t.Fatalf("no metrics: got %s, want skip", d.Action)
	}
	if d.DesiredReplicas != d.CurrentReplicas {
		t.Fatalf("no metrics: replicas changed from %d to %d", d.CurrentReplicas, d.DesiredReplicas)
	}
	if !hasReason(d, "never treated as zero load") {
		t.Fatalf("expected zero-load explanation, got %v", d.Reasons)
	}
}

// Cold-starting pods are excluded until they warm up.
func TestColdStartExcluded(t *testing.T) {
	cfg := testCfg()
	cfg.ColdStartSeconds = 30
	s := New(cfg, 1)

	// Only one pod, started 10s ago -> cold -> no usable metrics -> skip.
	s.Ingest([]Sample{{Time: t0, Pod: "p1", PodStart: t0.Add(-10 * time.Second), Value: 500}})
	d := s.Evaluate(t0)
	if d.Action != ActionSkip {
		t.Fatalf("cold pod only: got %s, want skip", d.Action)
	}
	if !strings.Contains(d.Excluded[0].Reason, "cold start") {
		t.Fatalf("expected cold-start reason, got %+v", d.Excluded)
	}

	// Same pod, now 31s old -> included, and the load triggers scale-up.
	s.Ingest([]Sample{{Time: t0.Add(21 * time.Second), Pod: "p1", PodStart: t0.Add(-10 * time.Second), Value: 500}})
	d = s.Evaluate(t0.Add(21 * time.Second))
	if d.Action != ActionScaleUp {
		t.Fatalf("warmed pod: got %s, want scale-up", d.Action)
	}
	if len(d.SamplesUsed) != 1 {
		t.Fatalf("warmed pod: %d samples used, want 1", len(d.SamplesUsed))
	}
}

// Scale-down protection: after load drops, the stabilization window holds
// replicas until the window has passed; then the rate limit and cooldown
// trickle the scale-down.
func TestScaleDownProtection(t *testing.T) {
	s := New(testCfg(), 4)

	// Establish a high recommendation inside the window.
	feed(s, pods(4), t0, 200)
	d := s.Evaluate(t0)
	if d.Action != ActionScaleUp || d.DesiredReplicas != 6 {
		t.Fatalf("setup: got %s to %d, want scale-up to 6", d.Action, d.DesiredReplicas)
	}

	// Load collapses 61s later. Raw desired would be low, but the window
	// still remembers the recent high recommendation -> hold.
	feed(s, pods(6), t0.Add(61*time.Second), 5)
	d = s.Evaluate(t0.Add(61 * time.Second))
	if d.Action != ActionHold || d.DesiredReplicas != 6 {
		t.Fatalf("protection: got %s to %d, want hold at 6", d.Action, d.DesiredReplicas)
	}
	if !hasReason(d, "scale-down stabilization") {
		t.Fatalf("expected stabilization reason, got %v", d.Reasons)
	}

	// After the 300s window has fully passed, scale-down starts — but only
	// one replica per action (rate limit).
	at := t0.Add(362 * time.Second)
	feed(s, pods(6), at, 5)
	d = s.Evaluate(at)
	if d.Action != ActionScaleDown || d.DesiredReplicas != 5 {
		t.Fatalf("after window: got %s to %d, want scale-down to 5 (rate-limited -1)", d.Action, d.DesiredReplicas)
	}
	if !hasReason(d, "rate limit") {
		t.Fatalf("expected rate-limit reason, got %v", d.Reasons)
	}

	// Immediately again: the scale-down cooldown blocks it.
	feed(s, pods(5), at.Add(30*time.Second), 5)
	d = s.Evaluate(at.Add(30 * time.Second))
	if d.Action != ActionHold || !hasReason(d, "cooldown") {
		t.Fatalf("down cooldown: got %s, reasons %v", d.Action, d.Reasons)
	}
}

// Bounds: desired counts are clamped to [min, max].
func TestMinMaxClamp(t *testing.T) {
	cfg := testCfg()
	cfg.MinReplicas = 2
	cfg.MaxReplicas = 3
	cfg.ScaleUpMaxStep = 10

	s := New(cfg, 3)
	feed(s, pods(3), t0, 500) // huge load
	d := s.Evaluate(t0)
	if d.DesiredReplicas != 3 || !hasReason(d, "max_replicas") {
		t.Fatalf("max clamp: got %d, reasons %v", d.DesiredReplicas, d.Reasons)
	}

	s.Reset(2)
	feed(s, pods(2), t0, 1) // almost no load
	d = s.Evaluate(t0)
	if d.DesiredReplicas != 2 || !hasReason(d, "min_replicas") {
		t.Fatalf("min clamp: got %d, reasons %v", d.DesiredReplicas, d.Reasons)
	}
}

// Tolerance band: a ratio inside ±tolerance causes no action.
func TestToleranceHold(t *testing.T) {
	s := New(testCfg(), 2)
	feed(s, pods(2), t0, 52) // ratio 1.04, inside ±0.1
	d := s.Evaluate(t0)
	if d.Action != ActionHold || !hasReason(d, "within tolerance") {
		t.Fatalf("tolerance: got %s, reasons %v", d.Action, d.Reasons)
	}
}

// Every decision must record the samples it used and at least one reason.
func TestDecisionAuditTrail(t *testing.T) {
	s := New(testCfg(), 2)
	feed(s, pods(2), t0, 120)
	d := s.Evaluate(t0)
	if len(d.SamplesUsed) != 2 {
		t.Fatalf("samples used = %d, want 2", len(d.SamplesUsed))
	}
	if len(d.Reasons) == 0 {
		t.Fatal("decision has no reasons")
	}
	if len(s.Decisions()) != 1 {
		t.Fatalf("decision history = %d, want 1", len(s.Decisions()))
	}
}

// Invalid configs are rejected.
func TestConfigValidation(t *testing.T) {
	s := New(testCfg(), 2)
	bad := testCfg()
	bad.MaxReplicas = 0
	if err := s.UpdateConfig(bad); err == nil {
		t.Fatal("expected validation error for max < min")
	}
	bad = testCfg()
	bad.TargetPerReplica = 0
	if err := s.UpdateConfig(bad); err == nil {
		t.Fatal("expected validation error for zero target")
	}
}
