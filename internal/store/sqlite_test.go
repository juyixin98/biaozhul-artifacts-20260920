package store

import (
	"context"
	"errors"
	"path/filepath"
	"testing"

	"scaler/internal/scaler"
)

func testCfg() scaler.Config {
	return scaler.Config{MinReplicas: 1, MaxReplicas: 10, TargetPct: 50, TolerancePct: 10, StableWindowSec: 60}
}

func newStore(t *testing.T) *Store {
	t.Helper()
	st, err := Open(context.Background(), ":memory:")
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { st.Close() })
	return st
}

func rec(workload, version string, metricMs, raw int) DecisionRecord {
	return DecisionRecord{
		Workload: workload, ConfigVersion: version, MetricTimeMs: int64(metricMs),
		CreatedAtMs: int64(metricMs), CurrentReplicas: 4,
		AvgUtilizationPct: 80, TargetPct: 50, Ratio: 1.6, RawProposed: raw,
		Stabilized: raw, FinalReplicas: raw, Action: "scaleup", Reasons: "X",
		Signature: "sig",
	}
}

func TestUpsertAndConfigVersions(t *testing.T) {
	st := newStore(t)
	ctx := context.Background()
	wl, err := st.UpsertWorkload(ctx, "web", "cfg_v1", testCfg(), 1000)
	if err != nil {
		t.Fatal(err)
	}
	if wl.ConfigVersion != "cfg_v1" {
		t.Fatalf("version=%q", wl.ConfigVersion)
	}
	got, err := st.GetWorkload(ctx, "web")
	if err != nil || got.MaxReplicas != 10 {
		t.Fatalf("get: %+v err=%v", got, err)
	}
	if _, err := st.GetWorkload(ctx, "missing"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("want ErrNotFound, got %v", err)
	}
}

func TestEventTimeRegressionGuard(t *testing.T) {
	st := newStore(t)
	ctx := context.Background()
	_, _ = st.UpsertWorkload(ctx, "web", "cfg_v1", testCfg(), 0)

	if _, err := st.InsertDecision(ctx, rec("web", "cfg_v1", 5000, 8), 8); err != nil {
		t.Fatal(err)
	}
	// Older event time: must be refused.
	if _, err := st.InsertDecision(ctx, rec("web", "cfg_v1", 4999, 3), 3); !errors.Is(err, ErrStaleMetric) {
		t.Fatalf("older time: want ErrStaleMetric, got %v", err)
	}
	// Equal event time: also refused (duplicate), latest decision is untouched.
	if _, err := st.InsertDecision(ctx, rec("web", "cfg_v1", 5000, 9), 9); !errors.Is(err, ErrStaleMetric) {
		t.Fatalf("equal time: want ErrStaleMetric, got %v", err)
	}
	latest, err := st.LatestDecision(ctx, "web")
	if err != nil {
		t.Fatal(err)
	}
	if latest.MetricTimeMs != 5000 || latest.FinalReplicas != 8 {
		t.Fatalf("newer decision was overwritten: %+v", latest)
	}
	// Strictly newer is accepted.
	if _, err := st.InsertDecision(ctx, rec("web", "cfg_v1", 5001, 7), 7); err != nil {
		t.Fatalf("newer time should be accepted: %v", err)
	}
}

func TestWindowEntriesBoundariesAndVersionIsolation(t *testing.T) {
	st := newStore(t)
	ctx := context.Background()
	_, _ = st.UpsertWorkload(ctx, "web", "cfg_v1", testCfg(), 0)

	// Entries at metric times 1,10001,20001,30001 plus one under a DIFFERENT
	// config version, which must be isolated (a config change starts a fresh
	// window). Insertion order must be time-ascending: the regression guard is
	// per-workload regardless of config version.
	for _, tc := range []struct {
		ts  int
		ver string
	}{{0, "cfg_v1"}, {10000, "cfg_v1"}, {15000, "cfg_v2"}, {20000, "cfg_v1"}, {30000, "cfg_v1"}} {
		if _, err := st.InsertDecision(ctx, rec("web", tc.ver, tc.ts+1, 5), 5); err != nil {
			t.Fatal(err)
		}
	}

	// Query window [1, 30001): now=30001, window=30000ms -> cutoff=1.
	got, err := st.WindowEntries(ctx, "web", "cfg_v1", 30001, 30000)
	if err != nil {
		t.Fatal(err)
	}
	var times []int64
	for _, e := range got {
		times = append(times, e.MetricTimeMs)
	}
	// Left edge inclusive (metric 1 >= cutoff 1); right edge exclusive
	// (30001 is not strictly older than now 30001).
	want := []int64{1, 10001, 20001}
	if len(times) != len(want) {
		t.Fatalf("got %v want %v", times, want)
	}
	for i := range want {
		if times[i] != want[i] {
			t.Fatalf("got %v want %v", times, want)
		}
	}

	// Other version sees only its own row.
	got2, err := st.WindowEntries(ctx, "web", "cfg_v2", 30001, 30000)
	if err != nil {
		t.Fatal(err)
	}
	if len(got2) != 1 || got2[0].MetricTimeMs != 15001 {
		t.Fatalf("version isolation broken: %+v", got2)
	}
}

func TestDecisionBindsConfigVersionAndMetricTime(t *testing.T) {
	st := newStore(t)
	ctx := context.Background()
	_, _ = st.UpsertWorkload(ctx, "web", "cfg_v1", testCfg(), 0)
	_, err := st.InsertDecision(ctx, rec("web", "cfg_v1", 5000, 8), 8)
	if err != nil {
		t.Fatal(err)
	}
	d, err := st.LatestDecision(ctx, "web")
	if err != nil {
		t.Fatal(err)
	}
	if d.ConfigVersion != "cfg_v1" || d.MetricTimeMs != 5000 {
		t.Fatalf("decision not bound to version/time: %+v", d)
	}
}

func TestPersistenceAcrossReopen(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "nested", "scaler.db")
	ctx := context.Background()

	st, err := Open(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = st.UpsertWorkload(ctx, "web", "cfg_v1", testCfg(), 0)
	if _, err := st.InsertDecision(ctx, rec("web", "cfg_v1", 5000, 8), 8); err != nil {
		t.Fatal(err)
	}
	if err := st.Close(); err != nil {
		t.Fatal(err)
	}

	st2, err := Open(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	defer st2.Close()
	wl, err := st2.GetWorkload(ctx, "web")
	if err != nil || wl.ConfigVersion != "cfg_v1" {
		t.Fatalf("config did not survive reopen: %+v err=%v", wl, err)
	}
	d, err := st2.LatestDecision(ctx, "web")
	if err != nil || d.FinalReplicas != 8 {
		t.Fatalf("decision did not survive reopen: %+v err=%v", d, err)
	}
	// Regression guard still active after reopen.
	if _, err := st2.InsertDecision(ctx, rec("web", "cfg_v1", 4000, 1), 1); !errors.Is(err, ErrStaleMetric) {
		t.Fatalf("guard lost after reopen: %v", err)
	}
}
