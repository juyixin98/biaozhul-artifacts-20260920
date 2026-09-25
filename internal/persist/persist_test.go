package persist_test

import (
	"os"
	"path/filepath"
	"testing"

	"cardinalitybudget/internal/persist"
	"cardinalitybudget/internal/store"
)

func sampleSnapshot(t *testing.T) store.Snapshot {
	t.Helper()
	st, err := store.New(store.Config{
		MaxSeriesPerMetric: 2,
		MaxMetricNames:     2,
		MaxMetricNameLen:   32,
		MaxLabelKeys:       4,
		MaxLabelKeyLen:     16,
		MaxLabelValueLen:   16,
	})
	if err != nil {
		t.Fatal(err)
	}
	st.Ingest(store.Sample{Metric: "m", Labels: map[string]string{"k": "v"}, Value: 1})
	st.Ingest(store.Sample{Metric: "m", Labels: map[string]string{"k": "w"}, Value: 2})
	st.Ingest(store.Sample{Metric: "m", Labels: map[string]string{"k": "overflow-x"}, Value: 3})
	return st.Export()
}

func TestSaveLoadRoundTrip(t *testing.T) {
	dir := t.TempDir()
	ps, err := persist.New(dir)
	if err != nil {
		t.Fatal(err)
	}
	if ok, err := ps.Exists(); err != nil || ok {
		t.Fatalf("Exists on fresh dir: %v %v", ok, err)
	}
	snap := sampleSnapshot(t)
	if err := ps.Save(snap); err != nil {
		t.Fatal(err)
	}
	if ok, err := ps.Exists(); err != nil || !ok {
		t.Fatalf("Exists after save: %v %v", ok, err)
	}
	got, err := ps.Load()
	if err != nil {
		t.Fatal(err)
	}
	if got.Version != store.SnapshotVersion || len(got.Metrics) != 1 {
		t.Fatalf("loaded snapshot wrong: %+v", got)
	}
	if got.Globals.SamplesAccepted != 3 {
		t.Fatalf("accepted=%d", got.Globals.SamplesAccepted)
	}

	// No leftover temp files in the directory.
	entries, _ := os.ReadDir(dir)
	for _, e := range entries {
		if filepath.Ext(e.Name()) == ".tmp" {
			t.Fatalf("temp file left behind: %s", e.Name())
		}
	}
}

func TestSaveCreatesMissingDir(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "nested", "data")
	ps, err := persist.New(dir)
	if err != nil {
		t.Fatal(err)
	}
	if err := ps.Save(sampleSnapshot(t)); err != nil {
		t.Fatal(err)
	}
}

func TestLoadRejectsGarbage(t *testing.T) {
	dir := t.TempDir()
	ps, _ := persist.New(dir)
	if err := os.WriteFile(filepath.Join(dir, "snapshot.json"), []byte("{not json"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := ps.Load(); err == nil {
		t.Fatal("expected parse error")
	}
}

func TestNewRejectsEmptyDir(t *testing.T) {
	if _, err := persist.New(""); err == nil {
		t.Fatal("expected error for empty dir")
	}
}
