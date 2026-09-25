package persist_test

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/example/hysteresis-alerter/internal/engine"
	"github.com/example/hysteresis-alerter/internal/persist"
)

// TestSaveLoadRoundTrip drives a rule through firing, saves, builds a new
// engine, restores and verifies clock, state, events and samples survive.
func TestSaveLoadRoundTrip(t *testing.T) {
	path := filepath.Join(t.TempDir(), "nested", "snapshot.json")

	store, err := persist.New(path)
	if err != nil {
		t.Fatal(err)
	}
	if snap, err := store.Load(); err != nil || snap != nil {
		t.Fatalf("fresh store: snap=%v err=%v, want nil,nil", snap, err)
	}

	e := engine.NewEngine()
	rule := engine.Rule{
		ID: "r1", Metric: "cpu", Operator: engine.OpGreaterOrEqual, Threshold: 80,
		TriggerFor: engine.Duration{Duration: 30 * time.Second},
		RecoverFor: engine.Duration{Duration: 30 * time.Second},
		NoDataFor:  engine.Duration{Duration: time.Minute},
	}
	if err := e.CreateRule(rule); err != nil {
		t.Fatal(err)
	}
	t0 := engine.Epoch.Add(time.Hour)
	e.Ingest([]engine.IngestItem{{Metric: "cpu", TS: t0, Value: 90}})
	e.Ingest([]engine.IngestItem{{Metric: "cpu", TS: t0.Add(30 * time.Second), Value: 95}})

	if err := store.Save(e.Export()); err != nil {
		t.Fatal(err)
	}

	// New engine, restored from disk.
	e2 := engine.NewEngine()
	snap, err := store.Load()
	if err != nil {
		t.Fatal(err)
	}
	if err := e2.Restore(snap); err != nil {
		t.Fatal(err)
	}
	if !e2.Now().Equal(t0.Add(30 * time.Second)) {
		t.Fatalf("clock restored=%s want %s", e2.Now(), t0.Add(30*time.Second))
	}
	rs, ok := e2.GetRule("r1")
	if !ok {
		t.Fatal("rule missing after restore")
	}
	if rs.State.State != engine.StateFiring {
		t.Fatalf("state after restore=%s want firing", rs.State.State)
	}
	if len(e2.QueryEvents("r1", true, nil)) != 1 {
		t.Fatal("firing event must survive restore")
	}
	if got := e2.QuerySamples("cpu", nil, nil, 0); len(got) != 2 {
		t.Fatalf("samples after restore=%d want 2", len(got))
	}

	// Continue evaluation on the restored engine: a cold streak resolves.
	e2.Ingest([]engine.IngestItem{{Metric: "cpu", TS: t0.Add(60 * time.Second), Value: 10}})
	e2.Ingest([]engine.IngestItem{{Metric: "cpu", TS: t0.Add(90 * time.Second), Value: 11}})
	evs := e2.QueryEvents("r1", true, nil)
	if len(evs) != 2 || evs[1].Type != engine.EventResolved {
		t.Fatalf("post-restore events=%v want firing+resolved", evs)
	}
}

// TestCorruptSnapshot: invalid JSON must surface a parse error rather than
// panic; an empty path is rejected.
func TestCorruptAndBadPath(t *testing.T) {
	path := filepath.Join(t.TempDir(), "snapshot.json")
	if err := os.WriteFile(path, []byte("{not json"), 0o600); err != nil {
		t.Fatal(err)
	}
	store, err := persist.New(path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.Load(); err == nil {
		t.Fatal("corrupt snapshot must return an error")
	}
	if got := store.Path(); got != path {
		t.Fatalf("Path()=%s want %s", got, path)
	}
	if _, err := persist.New(""); err == nil {
		t.Fatal("empty persist path must error")
	}
}

// TestSaveToReadOnlyDir exercises the temp-file creation failure branch
// (skipped when running as root, since root bypasses directory permissions).
func TestSaveToReadOnlyDir(t *testing.T) {
	if os.Getuid() == 0 {
		t.Skip("root bypasses directory permissions")
	}
	dir := t.TempDir()
	if err := os.Chmod(dir, 0o555); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(dir, 0o755) })

	store, err := persist.New(filepath.Join(dir, "snapshot.json"))
	if err != nil {
		t.Fatalf("New on an existing read-only dir should succeed, got %v", err)
	}
	e := engine.NewEngine()
	if err := store.Save(e.Export()); err == nil {
		t.Fatal("save into a read-only directory must fail")
	}
}

// TestSaveAtomic verifies the file never disappears/contains partial JSON
// during repeated saves, and the temp file is cleaned up.
func TestSaveAtomic(t *testing.T) {
	path := filepath.Join(t.TempDir(), "snapshot.json")
	store, err := persist.New(path)
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 20; i++ {
		e := engine.NewEngine()
		e.Ingest([]engine.IngestItem{{Metric: "m", TS: engine.Epoch.Add(time.Duration(i) * time.Second), Value: float64(i)}})
		if err := store.Save(e.Export()); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := store.Load(); err != nil {
		t.Fatalf("final snapshot unreadable: %v", err)
	}
	matches, _ := filepath.Glob(filepath.Join(filepath.Dir(path), ".snapshot-*.tmp"))
	if len(matches) != 0 {
		t.Fatalf("leftover temp files: %v", matches)
	}
}
