//go:build integration

package integration

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/example/crd-migration-demo/internal/migrator"
	"github.com/example/crd-migration-demo/test/testenv"
)

// TestFixtureExport_SuccessfulUpRecord generates a canonical successful
// migration record committed under fixtures/migration for documentation.
// It is gated by FIXTURE_OUT_DIR so normal test runs don't rewrite files.
func TestFixtureExport_SuccessfulUpRecord(t *testing.T) {
	dir := os.Getenv("FIXTURE_OUT_DIR")
	if dir == "" {
		t.Skip("set FIXTURE_OUT_DIR to export fixtures")
	}
	e := testenv.Start(t, "v1alpha1")
	ctx := context.Background()
	d := newDynamic(t, e)
	freshNS(t, ctx, e, "fx-ns")
	for i, name := range []string{"fx-1", "fx-2"} {
		mustCreate(t, ctx, d, gvrAlpha, map[string]any{
			"apiVersion": "timer.example.com/v1alpha1",
			"kind":       "Timer",
			"metadata":   map[string]any{"name": name, "namespace": "fx-ns"},
			"spec":       map[string]any{"intervalSeconds": int64(15 + i), "timeoutSeconds": int64(i)},
		})
	}
	e.SetStorageVersion(t, "v1")
	path := filepath.Join(dir, "migration-up-success.json")
	rec, err := migrator.Run(ctx, migrator.Options{
		ExtClient:  e.APIExtClient,
		Dynamic:    d,
		Direction:  "up",
		RecordPath: path,
	})
	if err != nil {
		t.Fatalf("migration: %v", err)
	}
	if rec.Migrated != 2 {
		t.Fatalf("expected 2 migrated, got %+v", rec)
	}
	t.Logf("wrote %s", path)
}
