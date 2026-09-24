package hpa

import (
	"context"
	"errors"
	"testing"
	"time"
)

// memStore is an in-memory hpa.Store for deterministic engine tests.
type memStore struct {
	lastTS    map[string]time.Time
	recs      map[string][]memRec
	decisions []*Decision
}

type memRec struct {
	version int64
	ts      time.Time
	desired int
}

func newMemStore() *memStore {
	return &memStore{lastTS: map[string]time.Time{}, recs: map[string][]memRec{}}
}

type memTx struct {
	s  *memStore
	id string
}

func (s *memStore) RunDecideTx(_ context.Context, scaler string, fn func(Tx) error) error {
	return fn(&memTx{s: s, id: scaler})
}

func (t *memTx) LastMetricTS() (time.Time, bool, error) {
	ts, ok := t.s.lastTS[t.id]
	return ts, ok, nil
}

func (t *memTx) SetLastMetricTS(ts time.Time) error {
	if last, ok := t.s.lastTS[t.id]; ok && ts.Before(last) {
		return ErrMetricRegression
	}
	t.s.lastTS[t.id] = ts
	return nil
}

func (t *memTx) InsertRecommendation(v int64, ts time.Time, raw int) error {
	t.s.recs[t.id] = append(t.s.recs[t.id], memRec{v, ts, raw})
	return nil
}

func (t *memTx) WindowMax(v int64, from, to time.Time) (int, int, error) {
	best, n := 0, 0
	for _, r := range t.s.recs[t.id] {
		if r.version == v && !r.ts.Before(from) && !r.ts.After(to) {
			n++
			if r.desired > best {
				best = r.desired
			}
		}
	}
	return best, n, nil
}

func (t *memTx) InsertDecision(d *Decision) error {
	t.s.decisions = append(t.s.decisions, d)
	return nil
}

func allReady(n int, util float64) []InstanceReport {
	out := make([]InstanceReport, n)
	for i := range out {
		u := util
		out[i] = InstanceReport{Name: "p", Ready: true, UtilizationPct: &u}
	}
	return out
}

// TestScaleDownStableWindow_MaxRecommendation verifies the core stable-window
// rule: scale-down takes the MAXIMUM raw recommendation inside the window.
func TestScaleDownStableWindow_MaxRecommendation(t *testing.T) {
	cfg := testCfg() // downscale window 300s, tolerance 10%, target 70%
	st := newMemStore()
	svc := NewService(st, []byte("k"))
	ctx := context.Background()

	// tick 0: load jumps to 100% at 4 replicas -> raw 6
	d, err := svc.Decide(ctx, cfg, DecisionRequest{
		CurrentReplicas: 4, MetricTimestamp: t0, Instances: allReady(4, 100),
	}, t0)
	if err != nil {
		t.Fatal(err)
	}
	if d.RawDesired != 6 || d.FinalDesired != 6 {
		t.Fatalf("t0 raw=%d final=%d, want 6/6", d.RawDesired, d.FinalDesired)
	}

	// orchestrator applies 6. 60s later load drops to 10% -> raw 1, but
	// window contains the recent "6", so the stable final stays at 6.
	d, err = svc.Decide(ctx, cfg, DecisionRequest{
		CurrentReplicas: 6, MetricTimestamp: t0.Add(60 * time.Second), Instances: allReady(6, 10),
	}, t0.Add(60*time.Second))
	if err != nil {
		t.Fatal(err)
	}
	if d.RawDesired != 1 || d.FinalDesired != 6 {
		t.Fatalf("t+60 raw=%d final=%d, want 1/6 (window max)", d.RawDesired, d.FinalDesired)
	}
	if d.WindowSamples != 2 {
		t.Fatalf("window samples=%d, want 2", d.WindowSamples)
	}

	// orchestrator still runs 6. At 120s..240s keep reporting low load;
	// window keeps the max 6 until the 6-record ages out at >300s.
	for i := 2; i <= 5; i++ {
		ts := t0.Add(time.Duration(i*60) * time.Second)
		d, err = svc.Decide(ctx, cfg, DecisionRequest{
			CurrentReplicas: 6, MetricTimestamp: ts, Instances: allReady(6, 10),
		}, ts)
		if err != nil {
			t.Fatal(err)
		}
		if d.FinalDesired != 6 {
			t.Fatalf("t+%dmin final=%d, window must still hold 6", i, d.FinalDesired)
		}
	}

	// 301s after the spike tick: [61s,361s] excludes the t0 record -> max 1.
	ts := t0.Add(361 * time.Second)
	d, err = svc.Decide(ctx, cfg, DecisionRequest{
		CurrentReplicas: 6, MetricTimestamp: ts, Instances: allReady(6, 10),
	}, ts)
	if err != nil {
		t.Fatal(err)
	}
	if d.RawDesired != 1 || d.FinalDesired != 1 || d.FinalAction != ScaleDown {
		t.Fatalf("t+361 raw=%d final=%d action=%s, want 1/1/ScaleDown",
			d.RawDesired, d.FinalDesired, d.FinalAction)
	}
}

// TestScaleUpStabilizationWindow verifies a configured upscale window also
// takes the max, while the default (0s) upscale window acts immediately.
func TestScaleUpStabilizationWindow(t *testing.T) {
	cfg := testCfg()
	cfg.ScaleUpStabilizationWindowSeconds = 120
	cfg.Fingerprint = FingerprintConfig(cfg)
	st := newMemStore()
	svc := NewService(st, []byte("k"))

	d, err := svc.Decide(context.Background(), cfg, DecisionRequest{
		CurrentReplicas: 4, MetricTimestamp: t0, Instances: allReady(4, 100),
	}, t0)
	if err != nil {
		t.Fatal(err)
	}
	if d.WindowSeconds != 120 || d.FinalDesired != 6 {
		t.Fatalf("window=%d final=%d", d.WindowSeconds, d.FinalDesired)
	}

	// brief dip into the tolerance band at +30s: raw holds at 4, but the
	// upscale window still contains the recommendation 6 -> final 6.
	d, err = svc.Decide(context.Background(), cfg, DecisionRequest{
		CurrentReplicas: 4, MetricTimestamp: t0.Add(30 * time.Second), Instances: allReady(4, 70),
	}, t0.Add(30*time.Second))
	if err != nil {
		t.Fatal(err)
	}
	if d.RawDesired != 4 || d.FinalDesired != 6 {
		t.Fatalf("dip raw=%d final=%d, want 4/6 (upscale window max)", d.RawDesired, d.FinalDesired)
	}
}

func TestMissingMetricsRecordNothingAndHold(t *testing.T) {
	cfg := testCfg()
	st := newMemStore()
	svc := NewService(st, []byte("k"))

	// first a high recommendation 6
	_, err := svc.Decide(context.Background(), cfg, DecisionRequest{
		CurrentReplicas: 4, MetricTimestamp: t0, Instances: allReady(4, 100),
	}, t0)
	if err != nil {
		t.Fatal(err)
	}

	// then every ready pod goes missing: hold, no recommendation recorded
	d, err := svc.Decide(context.Background(), cfg, DecisionRequest{
		CurrentReplicas: 4, MetricTimestamp: t0.Add(30 * time.Second),
		Instances: []InstanceReport{
			{Name: "p1", Ready: true, UtilizationPct: nil},
			{Name: "p2", Ready: true, UtilizationPct: nil},
			{Name: "p3", Ready: true, UtilizationPct: nil},
			{Name: "p4", Ready: true, UtilizationPct: nil},
		},
	}, t0.Add(30*time.Second))
	if err != nil {
		t.Fatal(err)
	}
	if d.FinalAction != Hold || d.RecommendationRecorded ||
		len(d.FinalReasons) != 1 || d.FinalReasons[0] != ReasonAllReadyMissing {
		t.Fatalf("missing: action=%s recorded=%v reasons=%v", d.FinalAction, d.RecommendationRecorded, d.FinalReasons)
	}
	if len(st.recs["app"]) != 1 {
		t.Fatalf("missing tick must not add a recommendation, got %d", len(st.recs["app"]))
	}

	// stale sample at +200s also holds without recording
	d, err = svc.Decide(context.Background(), cfg, DecisionRequest{
		CurrentReplicas: 4,
		MetricTimestamp: t0.Add(60 * time.Second), // older than clock t0+200s by >120s
		Instances:       allReady(4, 10),
	}, t0.Add(200*time.Second))
	if err != nil {
		t.Fatal(err)
	}
	if d.FinalAction != Hold || d.FinalReasons[0] != ReasonMetricsStale || d.RecommendationRecorded {
		t.Fatalf("stale: %v recorded=%v", d.FinalReasons, d.RecommendationRecorded)
	}
}

func TestEventTimeRegressionRejected(t *testing.T) {
	cfg := testCfg()
	st := newMemStore()
	svc := NewService(st, []byte("k"))

	_, err := svc.Decide(context.Background(), cfg, DecisionRequest{
		CurrentReplicas: 4, MetricTimestamp: t0.Add(100 * time.Second), Instances: allReady(4, 100),
	}, t0.Add(100*time.Second))
	if err != nil {
		t.Fatal(err)
	}
	// older event time — even with later wall/clock time — must be refused
	_, err = svc.Decide(context.Background(), cfg, DecisionRequest{
		CurrentReplicas: 4, MetricTimestamp: t0.Add(90 * time.Second), Instances: allReady(4, 10),
	}, t0.Add(110*time.Second))
	if !errors.Is(err, ErrMetricRegression) {
		t.Fatalf("want ErrMetricRegression, got %v", err)
	}
}

func TestConfigVersionOpensFreshWindow(t *testing.T) {
	cfg := testCfg() // version 1, window 300s
	st := newMemStore()
	svc := NewService(st, []byte("k"))

	_, err := svc.Decide(context.Background(), cfg, DecisionRequest{
		CurrentReplicas: 4, MetricTimestamp: t0, Instances: allReady(4, 100),
	}, t0)
	if err != nil {
		t.Fatal(err)
	}

	// new config version: WindowMax is filtered by version so it is empty
	cfg2 := cfg
	cfg2.Version = 2
	cfg2.Fingerprint = FingerprintConfig(cfg2)
	d, err := svc.Decide(context.Background(), cfg2, DecisionRequest{
		CurrentReplicas: 6, MetricTimestamp: t0.Add(10 * time.Second), Instances: allReady(6, 10),
	}, t0.Add(10*time.Second))
	if err != nil {
		t.Fatal(err)
	}
	if d.ConfigVersion != 2 || d.WindowSamples != 1 || d.RawDesired != 1 {
		t.Fatalf("version reset: samples=%d raw=%d (only the new tick counts)", d.WindowSamples, d.RawDesired)
	}
	if d.FinalDesired != 1 {
		t.Fatalf("new version must not be blocked by old version's max, final=%d", d.FinalDesired)
	}
}

func TestDecisionSignatureBinding(t *testing.T) {
	cfg := testCfg()
	st := newMemStore()
	svc := NewService(st, []byte("k"))
	d, err := svc.Decide(context.Background(), cfg, DecisionRequest{
		CurrentReplicas: 4, MetricTimestamp: t0, Instances: allReady(4, 100),
	}, t0)
	if err != nil {
		t.Fatal(err)
	}
	if d.Signature != SignDecision([]byte("k"), d) {
		t.Fatal("persisted signature must verify with the configured key")
	}
	if d.ConfigFingerprint == "" || d.ConfigVersion != 1 || d.MetricTimestamp.IsZero() {
		t.Fatal("decision must be bound to config version, fingerprint and metric time")
	}
}

func TestFinalMaxCap(t *testing.T) {
	// Raw 15 with maxReplicas 10 -> capped.
	cfg := testCfg()
	cfg.MaxReplicas = 10
	st := newMemStore()
	svc := NewService(st, []byte("k"))
	d, err := svc.Decide(context.Background(), cfg, DecisionRequest{
		CurrentReplicas: 10, MetricTimestamp: t0, Instances: allReady(10, 100),
	}, t0)
	if err != nil {
		t.Fatal(err)
	}
	// ceil(10*100/70)=15 -> raw capped to 10 already; final stays 10.
	if d.RawDesired != 10 || d.FinalDesired != 10 {
		t.Fatalf("cap raw=%d final=%d", d.RawDesired, d.FinalDesired)
	}
	if !contains(d.FinalReasons, ReasonCappedAtMax) {
		t.Fatalf("reasons=%v should mention max cap (raw)", d.FinalReasons)
	}
}

func contains(xs []string, v string) bool {
	for _, x := range xs {
		if x == v {
			return true
		}
	}
	return false
}
