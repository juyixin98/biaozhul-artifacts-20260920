package server_test

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"example.com/tenantiso/internal/auth"
	"example.com/tenantiso/internal/backend"
	"example.com/tenantiso/internal/cache"
	"example.com/tenantiso/internal/clock"
	"example.com/tenantiso/internal/jobs"
	"example.com/tenantiso/internal/server"
)

// testEnv bundles a running test server with its fakes.
type testEnv struct {
	t       *testing.T
	srv     *httptest.Server
	backend *backend.Fake
	cache   *cache.Cache
	mgr     *jobs.Manager
}

func newTestEnv(t *testing.T, cfg jobs.Config) *testEnv {
	t.Helper()
	be := backend.New()
	c := cache.New()
	mgr := jobs.NewManager(cfg, clock.Real{}, be, c)
	t.Cleanup(mgr.Close)
	srv := httptest.NewServer(server.New(mgr, c, be))
	t.Cleanup(srv.Close)
	return &testEnv{t: t, srv: srv, backend: be, cache: c, mgr: mgr}
}

// do issues an authenticated request and returns status code and body.
func (e *testEnv) do(method, path, tenant string, body any) (int, []byte) {
	e.t.Helper()
	var rdr io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			e.t.Fatalf("marshal body: %v", err)
		}
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, e.srv.URL+path, rdr)
	if err != nil {
		e.t.Fatalf("new request: %v", err)
	}
	if tenant != "" {
		req.Header.Set(auth.TenantHeader, tenant)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		e.t.Fatalf("do request: %v", err)
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, b
}

// submit posts a compute job and asserts the expected status code.
func (e *testEnv) submit(tenant string, body map[string]any, wantCode int) map[string]any {
	e.t.Helper()
	code, b := e.do(http.MethodPost, "/v1/compute", tenant, body)
	if code != wantCode {
		e.t.Fatalf("submit tenant=%s: got %d want %d: %s", tenant, code, wantCode, b)
	}
	var out map[string]any
	_ = json.Unmarshal(b, &out)
	return out
}

// waitStatus polls a job until it reaches one of the wanted statuses.
func (e *testEnv) waitStatus(tenant, id string, want ...string) map[string]any {
	e.t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		code, b := e.do(http.MethodGet, "/v1/jobs/"+id, tenant, nil)
		if code != http.StatusOK {
			e.t.Fatalf("get job %s: %d: %s", id, code, b)
		}
		var job map[string]any
		_ = json.Unmarshal(b, &job)
		for _, w := range want {
			if job["status"] == w {
				return job
			}
		}
		time.Sleep(5 * time.Millisecond)
	}
	e.t.Fatalf("job %s did not reach %v in time", id, want)
	return nil
}

func computeBody(key, payload string, iterations, memBytes int) map[string]any {
	return map[string]any{
		"key": key, "payload": payload, "iterations": iterations, "mem_bytes": memBytes,
	}
}

// TestCrossTenantSameKeyIsolation: two tenants compute under the same
// user key; each must read back only its own cached result.
func TestCrossTenantSameKeyIsolation(t *testing.T) {
	env := newTestEnv(t, jobs.Config{Workers: 2, MaxQueuePerTenant: 4, MemBudgetPerTenant: 1 << 20})

	jobA := env.submit("tenant-a", computeBody("shared-key", "payload-of-A", 10, 100), http.StatusAccepted)
	jobB := env.submit("tenant-b", computeBody("shared-key", "payload-of-B", 10, 100), http.StatusAccepted)
	env.waitStatus("tenant-a", jobA["id"].(string), "succeeded")
	env.waitStatus("tenant-b", jobB["id"].(string), "succeeded")

	codeA, valA := env.do(http.MethodGet, "/v1/cache/shared-key", "tenant-a", nil)
	codeB, valB := env.do(http.MethodGet, "/v1/cache/shared-key", "tenant-b", nil)
	if codeA != http.StatusOK || codeB != http.StatusOK {
		t.Fatalf("cache reads: A=%d B=%d", codeA, codeB)
	}
	if bytes.Equal(valA, valB) {
		t.Fatalf("isolation violated: tenants read identical values %x for same key", valA)
	}
	wantA, _ := env.backend.Compute(context.Background(), "payload-of-A", 10)
	if !bytes.Equal(valA, wantA) {
		t.Fatalf("tenant-a read wrong value: got %x want %x", valA, wantA)
	}
	if env.cache.Len() != 2 {
		t.Fatalf("expected 2 isolated cache entries, got %d", env.cache.Len())
	}
}

// TestTenantOverrideRejected: a tenant field in the body must be
// rejected; identity comes only from the auth context.
func TestTenantOverrideRejected(t *testing.T) {
	env := newTestEnv(t, jobs.Config{Workers: 1, MaxQueuePerTenant: 4, MemBudgetPerTenant: 1 << 20})
	for _, field := range []string{"tenant_id", "tenantId", "tenant"} {
		body := computeBody("k", "p", 1, 1)
		body[field] = "other-tenant"
		code, b := env.do(http.MethodPost, "/v1/compute", "tenant-a", body)
		if code != http.StatusBadRequest {
			t.Fatalf("body override via %q: got %d want 400: %s", field, code, b)
		}
	}
}

// TestMissingTenantRejected: no auth context means 401.
func TestMissingTenantRejected(t *testing.T) {
	env := newTestEnv(t, jobs.Config{Workers: 1, MaxQueuePerTenant: 4, MemBudgetPerTenant: 1 << 20})
	code, _ := env.do(http.MethodPost, "/v1/compute", "", computeBody("k", "p", 1, 1))
	if code != http.StatusUnauthorized {
		t.Fatalf("got %d want 401", code)
	}
}

// TestSingleTenantOverloadOthersServed: tenant A fills its queue and
// gets 429, while tenant B is still admitted and completes.
func TestSingleTenantOverloadOthersServed(t *testing.T) {
	env := newTestEnv(t, jobs.Config{Workers: 1, MaxQueuePerTenant: 2, MemBudgetPerTenant: 1 << 20})
	// Block the backend so tenant A's jobs pile up.
	env.backend.SetFaults(backend.Faults{Latency: 300 * time.Millisecond})

	env.submit("tenant-a", computeBody("a1", "p", 1, 1), http.StatusAccepted) // running
	env.submit("tenant-a", computeBody("a2", "p", 1, 1), http.StatusAccepted) // queued
	// Queue full for tenant A only.
	code, b := env.do(http.MethodPost, "/v1/compute", "tenant-a", computeBody("a3", "p", 1, 1))
	if code != http.StatusTooManyRequests {
		t.Fatalf("overloaded tenant: got %d want 429: %s", code, b)
	}
	// Tenant B is unaffected.
	jobB := env.submit("tenant-b", computeBody("b1", "p", 1, 1), http.StatusAccepted)

	env.backend.SetFaults(backend.Faults{})
	env.waitStatus("tenant-b", jobB["id"].(string), "succeeded")
}

// TestMemoryBudgetEnforced: a job exceeding the tenant memory budget is
// rejected with 429, and in-flight reservations are accounted.
func TestMemoryBudgetEnforced(t *testing.T) {
	env := newTestEnv(t, jobs.Config{Workers: 1, MaxQueuePerTenant: 8, MemBudgetPerTenant: 100})
	// Single job over budget.
	code, _ := env.do(http.MethodPost, "/v1/compute", "tenant-a", computeBody("big", "p", 1, 101))
	if code != http.StatusTooManyRequests {
		t.Fatalf("over-budget job: got %d want 429", code)
	}
	// Two jobs of 60 bytes: second exceeds the in-flight budget.
	env.backend.SetFaults(backend.Faults{Latency: 300 * time.Millisecond})
	env.submit("tenant-a", computeBody("m1", "p", 1, 60), http.StatusAccepted)
	code, _ = env.do(http.MethodPost, "/v1/compute", "tenant-a", computeBody("m2", "p", 1, 60))
	if code != http.StatusTooManyRequests {
		t.Fatalf("in-flight over-budget: got %d want 429", code)
	}
	env.backend.SetFaults(backend.Faults{})
}

// TestCancellation: a queued job can be canceled, which frees the
// tenant's queue slot and memory reservation for later jobs.
func TestCancellation(t *testing.T) {
	env := newTestEnv(t, jobs.Config{Workers: 1, MaxQueuePerTenant: 2, MemBudgetPerTenant: 100})
	env.backend.SetFaults(backend.Faults{Latency: 300 * time.Millisecond})

	env.submit("tenant-a", computeBody("c1", "p", 1, 50), http.StatusAccepted) // running
	job2 := env.submit("tenant-a", computeBody("c2", "p", 1, 50), http.StatusAccepted)
	id2 := job2["id"].(string)

	code, b := env.do(http.MethodDelete, "/v1/jobs/"+id2, "tenant-a", nil)
	if code != http.StatusOK {
		t.Fatalf("cancel: got %d: %s", code, b)
	}
	var canceled map[string]any
	_ = json.Unmarshal(b, &canceled)
	if canceled["status"] != "canceled" {
		t.Fatalf("cancel returned status %v", canceled["status"])
	}
	// Slot and memory freed: a new 50-byte job fits again.
	job3 := env.submit("tenant-a", computeBody("c3", "p", 1, 50), http.StatusAccepted)
	env.backend.SetFaults(backend.Faults{})
	env.waitStatus("tenant-a", job3["id"].(string), "succeeded")
	// Canceled job stays canceled.
	final := env.waitStatus("tenant-a", id2, "canceled")
	if final["status"] != "canceled" {
		t.Fatalf("canceled job changed state: %v", final["status"])
	}
}

// TestCrossTenantJobAccessDenied: tenant B cannot see or cancel
// tenant A's job.
func TestCrossTenantJobAccessDenied(t *testing.T) {
	env := newTestEnv(t, jobs.Config{Workers: 1, MaxQueuePerTenant: 4, MemBudgetPerTenant: 1 << 20})
	env.backend.SetFaults(backend.Faults{Latency: 300 * time.Millisecond})
	job := env.submit("tenant-a", computeBody("k", "p", 1, 1), http.StatusAccepted)
	id := job["id"].(string)

	code, _ := env.do(http.MethodGet, "/v1/jobs/"+id, "tenant-b", nil)
	if code != http.StatusNotFound {
		t.Fatalf("cross-tenant get: got %d want 404", code)
	}
	code, _ = env.do(http.MethodDelete, "/v1/jobs/"+id, "tenant-b", nil)
	if code != http.StatusNotFound {
		t.Fatalf("cross-tenant cancel: got %d want 404", code)
	}
	env.backend.SetFaults(backend.Faults{})
	env.waitStatus("tenant-a", id, "succeeded") // not canceled by B
}

// TestFaultInjection: injected failures surface as failed jobs; clearing
// the fault restores service.
func TestFaultInjection(t *testing.T) {
	env := newTestEnv(t, jobs.Config{Workers: 1, MaxQueuePerTenant: 4, MemBudgetPerTenant: 1 << 20})
	code, b := env.do(http.MethodPut, "/admin/faults", "", map[string]any{"latency": 0, "fail_next": 1})
	if code != http.StatusOK {
		t.Fatalf("set faults: %d: %s", code, b)
	}
	job := env.submit("tenant-a", computeBody("f1", "p", 1, 1), http.StatusAccepted)
	failed := env.waitStatus("tenant-a", job["id"].(string), "failed")
	if failed["error"] == "" {
		t.Fatalf("failed job carries no error: %v", failed)
	}
	job2 := env.submit("tenant-a", computeBody("f2", "p", 1, 1), http.StatusAccepted)
	env.waitStatus("tenant-a", job2["id"].(string), "succeeded")
}

// TestCacheMissIsPerTenant: a key computed only by tenant A is a miss
// for tenant B.
func TestCacheMissIsPerTenant(t *testing.T) {
	env := newTestEnv(t, jobs.Config{Workers: 1, MaxQueuePerTenant: 4, MemBudgetPerTenant: 1 << 20})
	job := env.submit("tenant-a", computeBody("only-a", "p", 1, 1), http.StatusAccepted)
	env.waitStatus("tenant-a", job["id"].(string), "succeeded")
	code, _ := env.do(http.MethodGet, "/v1/cache/only-a", "tenant-b", nil)
	if code != http.StatusNotFound {
		t.Fatalf("tenant-b cache read: got %d want 404", code)
	}
}

func Example_usage() {
	fmt.Println("see README.md for curl request samples")
	// Output: see README.md for curl request samples
}
