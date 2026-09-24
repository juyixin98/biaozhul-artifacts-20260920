package hpa

import (
	"testing"
	"time"
)

func testCfg() Config {
	c := WithDefaults(Config{
		ScalerID:          "app",
		Version:           1,
		TargetUtilization: 70,
		TolerancePct:      10,
		MinReplicas:       1,
		MaxReplicas:       10,
	})
	c.Fingerprint = FingerprintConfig(c)
	return c
}

var t0 = time.Date(2026, 9, 23, 12, 0, 0, 0, time.UTC)

func ptr(x float64) *float64 { return &x }

func TestFormula_BasicScaleUpDown(t *testing.T) {
	cfg := testCfg()
	// 4 pods at 100% util -> ceil(4 * 100 / 70) = 6
	a := Analyze(cfg, DecisionRequest{
		ScalerID: "app", CurrentReplicas: 4, MetricTimestamp: t0,
		Instances: []InstanceReport{
			{Name: "p1", Ready: true, UtilizationPct: ptr(100)},
			{Name: "p2", Ready: true, UtilizationPct: ptr(100)},
			{Name: "p3", Ready: true, UtilizationPct: ptr(100)},
			{Name: "p4", Ready: true, UtilizationPct: ptr(100)},
		},
	}, t0)
	if a.Hold {
		t.Fatalf("unexpected hold: %s", a.HoldReason)
	}
	if a.RawDesired != 6 {
		t.Fatalf("raw desired = %d, want 6", a.RawDesired)
	}
	if a.RawAction != ScaleUp {
		t.Fatalf("action = %s, want ScaleUp", a.RawAction)
	}

	// 6 pods at 30% -> ceil(6 * 30 / 70) = 3 (all ready, no safety weight)
	a = Analyze(cfg, DecisionRequest{
		ScalerID: "app", CurrentReplicas: 6, MetricTimestamp: t0,
		Instances: []InstanceReport{
			{Name: "p1", Ready: true, UtilizationPct: ptr(30)},
			{Name: "p2", Ready: true, UtilizationPct: ptr(30)},
			{Name: "p3", Ready: true, UtilizationPct: ptr(30)},
			{Name: "p4", Ready: true, UtilizationPct: ptr(30)},
			{Name: "p5", Ready: true, UtilizationPct: ptr(30)},
			{Name: "p6", Ready: true, UtilizationPct: ptr(30)},
		},
	}, t0)
	if a.RawDesired != 3 || a.RawAction != ScaleDown {
		t.Fatalf("got desired=%d action=%s, want 3/ScaleDown", a.RawDesired, a.RawAction)
	}
}

func TestToleranceBand(t *testing.T) {
	cfg := testCfg()
	// band is [0.9, 1.1] ratio => util [63, 77]
	for _, util := range []float64{63, 70, 77} {
		a := Analyze(cfg, DecisionRequest{
			ScalerID: "app", CurrentReplicas: 4, MetricTimestamp: t0,
			Instances: []InstanceReport{
				{Name: "p1", Ready: true, UtilizationPct: ptr(util)},
				{Name: "p2", Ready: true, UtilizationPct: ptr(util)},
				{Name: "p3", Ready: true, UtilizationPct: ptr(util)},
				{Name: "p4", Ready: true, UtilizationPct: ptr(util)},
			},
		}, t0)
		if a.Hold || !a.WithinTolerance || a.RawDesired != 4 {
			t.Fatalf("util=%.0f should hold inside band: desired=%d hold=%v", util, a.RawDesired, a.WithinTolerance)
		}
	}
	// just outside -> move
	a := Analyze(cfg, DecisionRequest{
		ScalerID: "app", CurrentReplicas: 4, MetricTimestamp: t0,
		Instances: []InstanceReport{
			{Name: "p1", Ready: true, UtilizationPct: ptr(78)},
			{Name: "p2", Ready: true, UtilizationPct: ptr(78)},
			{Name: "p3", Ready: true, UtilizationPct: ptr(78)},
			{Name: "p4", Ready: true, UtilizationPct: ptr(78)},
		},
	}, t0)
	if a.WithinTolerance || a.RawDesired != 5 {
		t.Fatalf("util 78 should scale up to 5, got %d (tol=%v)", a.RawDesired, a.WithinTolerance)
	}
}

func TestMissingMetricNeverZero(t *testing.T) {
	cfg := testCfg()
	// 4 ready pods; 3 report 10%, one metric missing -> assume 100%
	// eff = (10+10+10+100)/4 = 32.5 -> ceil(4*32.5/70)=2. Without the
	// missing-100% rule (i.e. filling with 0), it would be ceil(1)=1.
	a := Analyze(cfg, DecisionRequest{
		ScalerID: "app", CurrentReplicas: 4, MetricTimestamp: t0,
		Instances: []InstanceReport{
			{Name: "p1", Ready: true, UtilizationPct: ptr(10)},
			{Name: "p2", Ready: true, UtilizationPct: ptr(10)},
			{Name: "p3", Ready: true, UtilizationPct: ptr(10)},
			{Name: "p4", Ready: true, UtilizationPct: nil},
		},
	}, t0)
	if a.Hold || a.ReadyMissing != 1 || a.RawDesired != 2 {
		t.Fatalf("missing pod assumed 100%%: desired=%d hold=%v missing=%d", a.RawDesired, a.Hold, a.ReadyMissing)
	}

	// all ready pods missing -> hold, no recommendation; util must NOT be 0
	a = Analyze(cfg, DecisionRequest{
		ScalerID: "app", CurrentReplicas: 3, MetricTimestamp: t0,
		Instances: []InstanceReport{
			{Name: "p1", Ready: true, UtilizationPct: nil},
			{Name: "p2", Ready: true, UtilizationPct: nil},
			{Name: "p3", Ready: true, UtilizationPct: nil},
		},
	}, t0)
	if !a.Hold || a.HoldReason != ReasonAllReadyMissing || a.EffectiveUtilPct != 0 {
		t.Fatalf("all-missing should hold with %s, got hold=%v reason=%s", ReasonAllReadyMissing, a.Hold, a.HoldReason)
	}
}

func TestUnreadyInstances(t *testing.T) {
	cfg := testCfg()
	// Scaling up: 3 ready @ 100% + 1 not-ready (100% weight) -> eff 100 -> 6
	a := Analyze(cfg, DecisionRequest{
		ScalerID: "app", CurrentReplicas: 4, MetricTimestamp: t0,
		Instances: []InstanceReport{
			{Name: "p1", Ready: true, UtilizationPct: ptr(100)},
			{Name: "p2", Ready: true, UtilizationPct: ptr(100)},
			{Name: "p3", Ready: true, UtilizationPct: ptr(100)},
			{Name: "p4", Ready: false, UtilizationPct: nil},
		},
	}, t0)
	if a.UnreadyTotal != 1 || a.RawDesired != 6 || a.RawAction != ScaleUp {
		t.Fatalf("unready scale-up: desired=%d action=%s unready=%d", a.RawDesired, a.RawAction, a.UnreadyTotal)
	}

	// Scaling down: 3 ready @ 10% + 1 not-ready (0% weight) -> eff 7.5
	// ceil(4*7.5/70)=1
	a = Analyze(cfg, DecisionRequest{
		ScalerID: "app", CurrentReplicas: 4, MetricTimestamp: t0,
		Instances: []InstanceReport{
			{Name: "p1", Ready: true, UtilizationPct: ptr(10)},
			{Name: "p2", Ready: true, UtilizationPct: ptr(10)},
			{Name: "p3", Ready: true, UtilizationPct: ptr(10)},
			{Name: "p4", Ready: false, UtilizationPct: nil},
		},
	}, t0)
	if a.RawDesired != 1 || a.RawAction != ScaleDown {
		t.Fatalf("unready scale-down: desired=%d action=%s", a.RawDesired, a.RawAction)
	}

	// No ready instances at all -> hold
	a = Analyze(cfg, DecisionRequest{
		ScalerID: "app", CurrentReplicas: 2, MetricTimestamp: t0,
		Instances: []InstanceReport{
			{Name: "p1", Ready: false},
			{Name: "p2", Ready: false},
		},
	}, t0)
	if !a.Hold || a.HoldReason != ReasonNoReadyInstances {
		t.Fatalf("want %s, got hold=%v reason=%s", ReasonNoReadyInstances, a.Hold, a.HoldReason)
	}
}

func TestMaxReplicaCapAndStaleMetrics(t *testing.T) {
	cfg := testCfg() // max 10
	a := Analyze(cfg, DecisionRequest{
		ScalerID: "app", CurrentReplicas: 10, MetricTimestamp: t0,
		Instances: []InstanceReport{
			{Name: "p1", Ready: true, UtilizationPct: ptr(100)},
			{Name: "p2", Ready: true, UtilizationPct: ptr(100)},
			{Name: "p3", Ready: true, UtilizationPct: ptr(100)},
			{Name: "p4", Ready: true, UtilizationPct: ptr(100)},
			{Name: "p5", Ready: true, UtilizationPct: ptr(100)},
			{Name: "p6", Ready: true, UtilizationPct: ptr(100)},
			{Name: "p7", Ready: true, UtilizationPct: ptr(100)},
			{Name: "p8", Ready: true, UtilizationPct: ptr(100)},
			{Name: "p9", Ready: true, UtilizationPct: ptr(100)},
			{Name: "p10", Ready: true, UtilizationPct: ptr(100)},
		},
	}, t0)
	if !a.RawCappedMax || a.RawDesired != 10 || a.RawAction != Hold {
		t.Fatalf("max cap: capped=%v desired=%d action=%s", a.RawCappedMax, a.RawDesired, a.RawAction)
	}

	// stale metrics -> hold
	stale := t0.Add(-time.Duration(cfg.MetricFreshnessSeconds+1) * time.Second)
	a = Analyze(cfg, DecisionRequest{
		ScalerID: "app", CurrentReplicas: 4, MetricTimestamp: stale,
		Instances: []InstanceReport{
			{Name: "p1", Ready: true, UtilizationPct: ptr(99)},
			{Name: "p2", Ready: true, UtilizationPct: ptr(99)},
			{Name: "p3", Ready: true, UtilizationPct: ptr(99)},
			{Name: "p4", Ready: true, UtilizationPct: ptr(99)},
		},
	}, t0)
	if !a.Hold || a.HoldReason != ReasonMetricsStale {
		t.Fatalf("want %s, got hold=%v reason=%s", ReasonMetricsStale, a.Hold, a.HoldReason)
	}

	// no instances -> hold
	a = Analyze(cfg, DecisionRequest{ScalerID: "app", CurrentReplicas: 0, MetricTimestamp: t0}, t0)
	if !a.Hold || a.HoldReason != ReasonNoInstances {
		t.Fatalf("want %s, got %s", ReasonNoInstances, a.HoldReason)
	}
}

func TestZeroTargetValidation(t *testing.T) {
	bad := WithDefaults(Config{ScalerID: "x", MinReplicas: 1, MaxReplicas: 3})
	bad.TargetUtilization = 0 // force zero after defaults
	if err := Validate(&bad); err == nil {
		t.Fatal("zero target must be rejected by Validate (no division by zero)")
	}
}

func TestFingerprintAndSignatureAreReal(t *testing.T) {
	cfg := testCfg()
	fp := FingerprintConfig(cfg)
	if len(fp) != 64 { // SHA-256 hex
		t.Fatalf("fingerprint len=%d, want 64", len(fp))
	}
	cfg2 := cfg
	cfg2.MaxReplicas = 9
	if FingerprintConfig(cfg2) == fp {
		t.Fatal("different policy must yield different fingerprint")
	}

	d := &Decision{
		ScalerID: "app", ConfigVersion: 1, ConfigFingerprint: fp,
		MetricTimestamp: t0, DecidedAt: t0, CurrentReplicas: 4,
		RawDesired: 6, RawAction: ScaleUp, WindowedDesired: 6,
		FinalDesired: 6, FinalAction: ScaleUp,
	}
	s1 := SignDecision([]byte("secret"), d)
	s2 := SignDecision([]byte("secret"), d)
	if len(s1) != 64 || s1 != s2 {
		t.Fatal("HMAC must be 64 hex chars and deterministic")
	}
	if SignDecision([]byte("other"), d) == s1 {
		t.Fatal("different keys must produce different HMACs")
	}
}
