// Package integration contains end-to-end tests that exercise the HTTP API,
// the immutable catalog/reassignment machinery and the authorization model
// against a real MySQL instance.
//
// They are skipped unless TEST_MYSQL_DSN is set; the Makefile target
// `make test-integration` points it at the docker-compose/CI MySQL.
package integration

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
	"time"

	"gorm.io/gorm"

	"geoterritory/internal/api"
	"geoterritory/internal/config"
	"geoterritory/internal/models"
	"geoterritory/internal/store"
	"geoterritory/internal/worker"
)

type testEnv struct {
	db     *gorm.DB
	server *httptest.Server
	worker *worker.Worker
	keyA   string
	keyB   string
}

var env *testEnv

func TestMain(m *testing.M) {
	dsn := os.Getenv("TEST_MYSQL_DSN")
	if dsn == "" {
		fmt.Println("SKIP integration tests: set TEST_MYSQL_DSN (e.g. root:root@tcp(127.0.0.1:33061)/geoterritory_test?...multiStatements=true)")
		os.Exit(0)
	}
	db, err := store.Open(dsn)
	if err != nil {
		fmt.Printf("cannot open mysql: %v\n", err)
		os.Exit(1)
	}
	cleanSchema(db)
	if err := store.Migrate(db, "../../migrations"); err != nil {
		fmt.Printf("migrate: %v\n", err)
		os.Exit(1)
	}

	cfg := config.Config{LockTimeoutS: 10, BatchSize: 50, StaleJobAfter: 2 * time.Second}
	// Unique per-run org names to avoid unique-key collisions if cleanup lags.
	suffix := fmt.Sprintf("%d", time.Now().UnixNano())
	env = &testEnv{
		db:     db,
		keyA:   "test-key-a-" + suffix,
		keyB:   "test-key-b-" + suffix,
		worker: worker.New(db, 200*time.Millisecond, cfg.BatchSize, cfg.StaleJobAfter, cfg.LockTimeoutS),
	}
	if err := store.SeedOrganization(db, "org-a-"+suffix, env.keyA); err != nil {
		fmt.Printf("seed a: %v\n", err)
		os.Exit(1)
	}
	if err := store.SeedOrganization(db, "org-b-"+suffix, env.keyB); err != nil {
		fmt.Printf("seed b: %v\n", err)
		os.Exit(1)
	}
	env.server = httptest.NewServer(api.NewServer(db, cfg).Router())
	code := m.Run()
	env.server.Close()
	os.Exit(code)
}

// cleanSchema drops all app tables so migrations run from scratch.
func cleanSchema(db *gorm.DB) {
	tables := []string{
		"point_assignments", "reassignment_jobs", "catalog_entries",
		"region_catalog_state", "region_versions", "regions",
		"points", "organizations", "schema_migrations",
	}
	for _, t := range tables {
		_ = db.Exec("DROP TABLE IF EXISTS " + t).Error
	}
}

func orgIDByKey(t *testing.T, key string) uint64 {
	t.Helper()
	o, err := store.OrganizationByAPIKey(env.db, key)
	if err != nil {
		t.Fatalf("resolve org: %v", err)
	}
	return o.ID
}

// ---- tiny JSON HTTP client ----

func doJSON(t *testing.T, method, path, key string, body any) (int, map[string]any) {
	t.Helper()
	var buf bytes.Buffer
	if body != nil {
		if err := json.NewEncoder(&buf).Encode(body); err != nil {
			t.Fatal(err)
		}
	}
	req, err := http.NewRequest(method, env.server.URL+path, &buf)
	if err != nil {
		t.Fatal(err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if key != "" {
		req.Header.Set("X-API-Key", key)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	dec := json.NewDecoder(resp.Body)
	_ = dec.Decode(&out)
	if out == nil {
		out = map[string]any{}
	}
	return resp.StatusCode, out
}

// waitForJob polls a job until it reaches DONE/FAILED or the deadline passes.
func waitForJob(t *testing.T, key string, jobID float64) map[string]any {
	t.Helper()
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		status, body := doJSON(t, http.MethodGet, fmt.Sprintf("/v1/jobs/%.0f", jobID), key, nil)
		if status != http.StatusOK {
			t.Fatalf("get job status=%d body=%v", status, body)
		}
		job := body["job"].(map[string]any)
		switch job["status"] {
		case models.JobDone:
			return job
		case models.JobFailed:
			t.Fatalf("job failed: %v", job["error"])
		}
		// Nudge the worker deterministically.
		env.worker.RunOnce(context.Background())
		time.Sleep(30 * time.Millisecond)
	}
	t.Fatal("job did not finish in time")
	return nil
}

func runWorkerUntilIdle(ctx context.Context) {
	for env.worker.RunOnce(ctx) {
	}
}

func mustCurrentVersion(t *testing.T, key string) int {
	t.Helper()
	_, body := doJSON(t, http.MethodGet, "/v1/catalog", key, nil)
	return int(body["current_version"].(float64))
}

// squareRegion builds an axis-aligned square ring around (lat,lng).
func squareRegion(name string, priority int, lat0, lng0, half float64) map[string]any {
	return map[string]any{
		"name":     name,
		"priority": priority,
		"vertices": []map[string]float64{
			{"lat": lat0 - half, "lng": lng0 - half},
			{"lat": lat0 + half, "lng": lng0 - half},
			{"lat": lat0 + half, "lng": lng0 + half},
			{"lat": lat0 - half, "lng": lng0 + half},
		},
	}
}

// newOrg seeds a fresh, isolated organization and returns its API key.
func newOrg(t *testing.T) string {
	t.Helper()
	key := "test-key-" + uniq()
	if err := store.SeedOrganization(env.db, "org-"+uniq(), key); err != nil {
		t.Fatalf("seed org: %v", err)
	}
	return key
}
