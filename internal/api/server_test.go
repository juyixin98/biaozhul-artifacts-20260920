package api_test

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"

	"github.com/example/artifact-promotion/internal/api"
	"github.com/example/artifact-promotion/internal/blob"
	"github.com/example/artifact-promotion/internal/core"
	"github.com/example/artifact-promotion/internal/migrate"
	"github.com/jackc/pgx/v5/pgxpool"
)

// API tests use their own database so they can run in parallel with the
// core package tests (go test runs packages concurrently).
var (
	once sync.Once
	srv  *httptest.Server
	err  error
)

func server(t *testing.T) *httptest.Server {
	t.Helper()
	once.Do(func() {
		dsn := os.Getenv("TEST_API_DATABASE_URL")
		if dsn == "" {
			dsn = "postgres://promote:promote@localhost:5432/promotion_b_api_test?sslmode=disable"
		}
		ctx := context.Background()
		pool, e := pgxpool.New(ctx, dsn)
		if e != nil {
			err = e
			return
		}
		if e := migrate.Apply(ctx, pool); e != nil {
			err = e
			return
		}
		if _, e := pool.Exec(ctx, `TRUNCATE environments, artifacts, evidence, policies, approvals, attempts, env_history`); e != nil {
			err = e
			return
		}
		blobs, e := blob.New(t.TempDir())
		if e != nil {
			err = e
			return
		}
		svc := core.NewService(pool, blobs, core.Hooks{})
		srv = httptest.NewServer(api.NewRouter(svc))
	})
	if err != nil {
		t.Skipf("test database unavailable: %v", err)
	}
	return srv
}

func do(t *testing.T, method, path string, body any) (int, map[string]any) {
	t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req, err := http.NewRequest(method, srv.URL+path, rdr)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("do %s %s: %v", method, path, err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func TestAPIEndToEnd(t *testing.T) {
	server(t)

	// Environments.
	st, _ := do(t, "POST", "/v1/envs", map[string]any{"name": "api-test", "retention_keep": 5, "retention_max_age_days": 30})
	if st != http.StatusCreated {
		t.Fatalf("create env api-test: %d", st)
	}
	st, _ = do(t, "POST", "/v1/envs", map[string]any{"name": "api-staging"})
	if st != http.StatusCreated {
		t.Fatalf("create env api-staging: %d", st)
	}

	// Ingest an artifact (raw bytes) into api-test at generation 0.
	req, _ := http.NewRequest("POST", srv.URL+"/v1/envs/api-test/artifacts?expected_generation=0",
		strings.NewReader("api-artifact-bytes-v1"))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("ingest: %v", err)
	}
	var ingestAttempt map[string]any
	json.NewDecoder(resp.Body).Decode(&ingestAttempt)
	resp.Body.Close()
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("ingest status %d: %v", resp.StatusCode, ingestAttempt)
	}
	digest := ingestAttempt["artifact_digest"].(string)
	if !strings.HasPrefix(digest, "sha256:") {
		t.Fatalf("digest not immutable form: %s", digest)
	}

	// Evidence + policy + approval.
	st, ev := do(t, "POST", "/v1/evidence", map[string]any{
		"id": "api-ev", "artifact_digest": digest, "suite": "integration", "passed": true,
		"report": map[string]any{"tests": 7},
	})
	if st != http.StatusCreated {
		t.Fatalf("evidence: %d %v", st, ev)
	}
	st, pol := do(t, "POST", "/v1/policies", map[string]any{
		"id": "api-pol", "source_env": "api-test", "target_env": "api-staging",
		"required_suite": "integration", "min_evidence_version": 1,
	})
	if st != http.StatusCreated {
		t.Fatalf("policy: %d %v", st, pol)
	}
	st, ap := do(t, "POST", "/v1/approvals", map[string]any{
		"environment": "api-staging", "artifact_digest": digest, "approver": "qa-lead",
	})
	if st != http.StatusCreated {
		t.Fatalf("approval: %d %v", st, ap)
	}

	// Promote with a wrong expected generation → 409 generation_conflict.
	st, bad := do(t, "POST", "/v1/promotions", map[string]any{
		"source_env": "api-test", "target_env": "api-staging", "digest": digest,
		"evidence_id": "api-ev", "evidence_version": 1,
		"policy_id": "api-pol", "policy_version": 1,
		"approval_id": ap["id"], "expected_generation": 99,
	})
	if st != http.StatusConflict || bad["failure_reason"] != "generation_conflict" {
		t.Fatalf("expected 409 generation_conflict, got %d %v", st, bad)
	}

	// Promote correctly.
	st, prom := do(t, "POST", "/v1/promotions", map[string]any{
		"source_env": "api-test", "target_env": "api-staging", "digest": digest,
		"evidence_id": "api-ev", "evidence_version": ev["version"],
		"policy_id": "api-pol", "policy_version": pol["version"],
		"approval_id": ap["id"], "expected_generation": 0,
	})
	if st != http.StatusCreated || prom["status"] != "succeeded" {
		t.Fatalf("promote: %d %v", st, prom)
	}

	// Environment pointer moved.
	st, env := do(t, "GET", "/v1/envs/api-staging", nil)
	if st != http.StatusOK || env["current_digest"] != digest || env["generation"].(float64) != 1 {
		t.Fatalf("env state: %d %v", st, env)
	}

	// Attempt evidence retrievable by id, with the full step log.
	st, att := do(t, "GET", "/v1/attempts/"+prom["id"].(string), nil)
	if st != http.StatusOK || att["status"] != "succeeded" {
		t.Fatalf("attempt: %d %v", st, att)
	}
	steps, _ := att["steps"].([]any)
	if len(steps) < 7 {
		t.Fatalf("expected full step evidence, got %d steps: %v", len(steps), steps)
	}

	// Rolling back to the currently-live digest is rejected.
	st, rb := do(t, "POST", "/v1/rollbacks", map[string]any{
		"environment": "api-staging", "digest": digest, "expected_generation": 1,
	})
	if st != http.StatusConflict || rb["failure_reason"] != "already_current" {
		t.Fatalf("rollback to current should be rejected: %d %v", st, rb)
	}
}
