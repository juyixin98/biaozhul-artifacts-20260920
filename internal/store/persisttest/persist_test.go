package store_test

import (
	"testing"

	"cardinalitybudget/internal/persist"
	"cardinalitybudget/internal/store"
)

func cfg() store.Config {
	return store.Config{
		MaxSeriesPerMetric: 5,
		MaxMetricNames:     3,
		MaxMetricNameLen:   32,
		MaxLabelKeys:       4,
		MaxLabelKeyLen:     16,
		MaxLabelValueLen:   16,
	}
}

// TestRestartRecovery fills a store (including overflow), snapshots it,
// restores into a fresh store, and asserts that every counter and series is
// identical and that ingestion continues correctly after restart.
func TestRestartRecovery(t *testing.T) {
	dir := t.TempDir()
	ps, err := persist.New(dir)
	if err != nil {
		t.Fatal(err)
	}
	if ok, _ := ps.Exists(); ok {
		t.Fatal("fresh dir should have no snapshot")
	}

	st1, _ := store.New(cfg())
	// 5 normal series for m1 + 8 overflow samples.
	for i := 0; i < 5; i++ {
		st1.Ingest(store.Sample{Metric: "m1", Labels: map[string]string{"k": string(rune('a' + i))}, Value: float64(i)})
	}
	for i := 0; i < 8; i++ {
		st1.Ingest(store.Sample{Metric: "m1", Labels: map[string]string{"atk": string(rune('A' + i))}, Value: 10})
	}
	// Second metric with truncation counter activity (global counter).
	st1.Ingest(store.Sample{Metric: "m2", Labels: map[string]string{"k": "0123456789ABCDEF-extra"}, Value: 1})
	// One rejected sample.
	st1.Ingest(store.Sample{Metric: ""})

	before := st1.Stats()
	if err := ps.Save(st1.Export()); err != nil {
		t.Fatal(err)
	}
	if ok, _ := ps.Exists(); !ok {
		t.Fatal("snapshot missing after save")
	}

	snap, err := ps.Load()
	if err != nil {
		t.Fatal(err)
	}
	wantCfg := cfg()
	st2, err := store.Restore(snap, &wantCfg)
	if err != nil {
		t.Fatalf("restore: %v", err)
	}
	after := st2.Stats()

	if before != after {
		t.Fatalf("stats differ after restart:\nbefore=%+v\nafter =%+v", before, after)
	}

	for _, name := range []string{"m1", "m2"} {
		v1, ok1 := st1.Metric(name, true)
		v2, ok2 := st2.Metric(name, true)
		if !ok1 || !ok2 {
			t.Fatalf("metric %s missing (orig=%v restored=%v)", name, ok1, ok2)
		}
		if v1.Count != v2.Count || v1.NormalSeries != v2.NormalSeries || v1.OverflowCount != v2.OverflowCount {
			t.Fatalf("%s view differs:\norig=%+v\nnew =%+v", name, v1, v2)
		}
		if len(v1.Series) != len(v2.Series) {
			t.Fatalf("%s series count %d != %d", name, len(v2.Series), len(v1.Series))
		}
	}

	// Post-restart ingestion: an old combination still lands in the same
	// series, and new combinations keep overflowing.
	o := st2.Ingest(store.Sample{Metric: "m1", Labels: map[string]string{"k": "a"}, Value: 1})
	if o.Overflow || o.NewSeries {
		t.Fatalf("pre-restart series not restored correctly: %+v", o)
	}
	o = st2.Ingest(store.Sample{Metric: "m1", Labels: map[string]string{"atk": "brand-new"}, Value: 1})
	if !o.Overflow {
		t.Fatalf("expected overflow after restore, got %+v", o)
	}

	// A second save/restore round-trip must stay stable.
	if err := ps.Save(st2.Export()); err != nil {
		t.Fatal(err)
	}
	snap2, err := ps.Load()
	if err != nil {
		t.Fatal(err)
	}
	st3, err := store.Restore(snap2, &wantCfg)
	if err != nil {
		t.Fatal(err)
	}
	if st3.Stats() != st2.Stats() {
		t.Fatal("second restore mismatch")
	}
}

func TestRestoreRejectsCorruptAndMismatched(t *testing.T) {
	st1, _ := store.New(cfg())
	st1.Ingest(store.Sample{Metric: "m", Labels: map[string]string{"k": "v"}})

	// Mismatched running config is rejected.
	snap := st1.Export()
	different := cfg()
	different.MaxSeriesPerMetric = 99
	if _, err := store.Restore(snap, &different); err == nil {
		t.Fatal("expected config-mismatch error")
	}

	// Tampered snapshot: metric count inconsistent with series totals.
	snap.Metrics[0].Count++
	if _, err := store.Restore(snap, nil); err == nil {
		t.Fatal("expected conservation error on tampered snapshot")
	}
}
