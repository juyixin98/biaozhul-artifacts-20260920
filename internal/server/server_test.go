package server_test

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"tenantiso/internal/cache"
	"tenantiso/internal/clock"
	"tenantiso/internal/engine"
	"tenantiso/internal/fakestore"
	"tenantiso/internal/server"
)

func newTestServer(t *testing.T, limits engine.Limits, workers int) *httptest.Server {
	t.Helper()
	clk := clock.Real{}
	store := fakestore.New(clk)
	c := cache.New(store, "compute")
	eng := engine.NewScheduler(clk, c, limits, workers)
	eng.Start()
	t.Cleanup(eng.Stop)
	srv := httptest.NewServer(server.New(eng, c, clk, limits))
	t.Cleanup(srv.Close)
	return srv
}

func defaultLimits() engine.Limits {
	return engine.Limits{
		MaxQueueDepth:       4,
		MaxInFlightJobs:     2,
		MaxInFlightMemBytes: 16 << 20,
		MaxJobMemBytes:      8 << 20,
		MaxWork:             1_000_000_000,
	}
}

func do(t *testing.T, method, url, tenant string, body []byte) (int, map[string]any) {
	t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		rdr = bytes.NewReader(body)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req, err := http.NewRequest(method, url, rdr)
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	if tenant != "" {
		req.Header.Set("X-Tenant-ID", tenant)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("%s %s: %v", method, url, err)
	}
	defer resp.Body.Close()
	var parsed map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&parsed)
	return resp.StatusCode, parsed
}

func postCompute(t *testing.T, base, tenant, body string) (int, map[string]any) {
	t.Helper()
	return do(t, "POST", base+"/v1/compute", tenant, []byte(body))
}

func awaitJobStatus(t *testing.T, base, tenant, id, want string, timeout time.Duration) map[string]any {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		code, snap := do(t, "GET", base+"/v1/jobs/"+id, tenant, nil)
		if code == 200 && snap["status"] == want {
			return snap
		}
		time.Sleep(10 * time.Millisecond)
	}
	_, snap := do(t, "GET", base+"/v1/jobs/"+id, tenant, nil)
	t.Fatalf("job %s did not reach %q within %s (now %v)", id, want, timeout, snap["status"])
	return nil
}

func TestCrossTenantSameKeyIsolation(t *testing.T) {
	srv := newTestServer(t, defaultLimits(), 2)

	code, jobA := do(t, "POST", srv.URL+"/v1/compute?wait=true", "tenant-a",
		[]byte(`{"key":"shared","work":100000,"mem_bytes":4096}`))
	if code != 200 || jobA["status"] != "completed" {
		t.Fatalf("tenant-a compute: code=%d job=%v", code, jobA)
	}
	code, jobB := do(t, "POST", srv.URL+"/v1/compute?wait=true", "tenant-b",
		[]byte(`{"key":"shared","work":200000,"mem_bytes":4096}`))
	if code != 200 || jobB["status"] != "completed" {
		t.Fatalf("tenant-b compute: code=%d job=%v", code, jobB)
	}

	if jobA["result"] == jobB["result"] {
		t.Fatal("different work must yield different results")
	}
	if jobA["cache_key"] != "compute|tenant-a|shared" || jobB["cache_key"] != "compute|tenant-b|shared" {
		t.Fatalf("cache keys must embed the isolation domain and tenant: %v %v",
			jobA["cache_key"], jobB["cache_key"])
	}

	code, gotA := do(t, "GET", srv.URL+"/v1/cache/shared", "tenant-a", nil)
	if code != 200 || gotA["value"] != jobA["result"] {
		t.Fatalf("tenant-a readback: code=%d got=%v want=%v", code, gotA["value"], jobA["result"])
	}
	code, gotB := do(t, "GET", srv.URL+"/v1/cache/shared", "tenant-b", nil)
	if code != 200 || gotB["value"] != jobB["result"] {
		t.Fatalf("tenant-b readback: code=%d got=%v want=%v", code, gotB["value"], jobB["result"])
	}
}

func TestTenantOverrideRejected(t *testing.T) {
	srv := newTestServer(t, defaultLimits(), 1)

	for _, body := range []string{
		`{"key":"k","work":1000,"tenant_id":"tenant-b"}`,
		`{"key":"k","work":1000,"tenantId":"tenant-b"}`,
	} {
		code, resp := postCompute(t, srv.URL, "tenant-a", body)
		if code != http.StatusBadRequest {
			t.Fatalf("body %s: expected 400, got %d (%v)", body, code, resp)
		}
		errObj, _ := resp["error"].(map[string]any)
		if errObj["code"] != "TENANT_OVERRIDE_REJECTED" {
			t.Fatalf("body %s: expected TENANT_OVERRIDE_REJECTED, got %v", body, errObj["code"])
		}
	}
}

func TestAuthRequired(t *testing.T) {
	srv := newTestServer(t, defaultLimits(), 1)
	code, _ := postCompute(t, srv.URL, "", `{"key":"k","work":1000}`)
	if code != http.StatusUnauthorized {
		t.Fatalf("expected 401 without tenant header, got %d", code)
	}
	code, _ = do(t, "GET", srv.URL+"/v1/cache/k", "bad tenant!", nil)
	if code != http.StatusUnauthorized {
		t.Fatalf("expected 401 for invalid tenant id, got %d", code)
	}
}

func TestSingleTenantOverloadOthersServed(t *testing.T) {
	limits := defaultLimits()
	limits.MaxQueueDepth = 2
	limits.MaxInFlightJobs = 1
	srv := newTestServer(t, limits, 1)

	const flood = 10
	codes := make([]int, flood)
	var wg sync.WaitGroup
	for i := 0; i < flood; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			code, _ := postCompute(t, srv.URL, "tenant-heavy",
				`{"key":"flood","work":100000000,"mem_bytes":1024}`)
			codes[i] = code
		}(i)
	}
	wg.Wait()

	accepted, rejected := 0, 0
	for _, c := range codes {
		switch c {
		case http.StatusAccepted:
			accepted++
		case http.StatusTooManyRequests:
			rejected++
		default:
			t.Fatalf("unexpected status %d", c)
		}
	}
	if rejected == 0 {
		t.Fatalf("expected queue-full rejections, got accepted=%d rejected=0", accepted)
	}
	if accepted > limits.MaxQueueDepth+limits.MaxInFlightJobs {
		t.Fatalf("accepted %d exceeds queue+inflight capacity", accepted)
	}

	// Another tenant is still served while tenant-heavy is saturated.
	code, job := do(t, "POST", srv.URL+"/v1/compute?wait=true&timeout_ms=20000", "tenant-quiet",
		[]byte(`{"key":"q","work":100000,"mem_bytes":1024}`))
	if code != 200 || job["status"] != "completed" {
		t.Fatalf("tenant-quiet not served during overload: code=%d job=%v", code, job)
	}
}

func TestMemoryBudgetIsolatesTenants(t *testing.T) {
	limits := defaultLimits()
	limits.MaxInFlightMemBytes = 4096
	limits.MaxJobMemBytes = 4096
	srv := newTestServer(t, limits, 2)

	// tenant-a occupies its whole memory budget with a long job.
	code, jobA := postCompute(t, srv.URL, "tenant-a",
		`{"key":"big","work":500000000,"mem_bytes":4096}`)
	if code != http.StatusAccepted {
		t.Fatalf("tenant-a big job: %d", code)
	}
	awaitJobStatus(t, srv.URL, "tenant-a", fmt.Sprint(jobA["job_id"]), "running", 5*time.Second)

	// tenant-b's job is not blocked by tenant-a's budget consumption.
	code, jobB := do(t, "POST", srv.URL+"/v1/compute?wait=true&timeout_ms=20000", "tenant-b",
		[]byte(`{"key":"small","work":100000,"mem_bytes":4096}`))
	if code != 200 || jobB["status"] != "completed" {
		t.Fatalf("tenant-b blocked by tenant-a budget: code=%d job=%v", code, jobB)
	}
}

func TestCancellation(t *testing.T) {
	srv := newTestServer(t, defaultLimits(), 1)

	code, job := postCompute(t, srv.URL, "tenant-a",
		`{"key":"long","work":900000000,"mem_bytes":1024}`)
	if code != http.StatusAccepted {
		t.Fatalf("submit: %d", code)
	}
	id := fmt.Sprint(job["job_id"])

	// Cross-tenant cancel and read are refused.
	if code, _ := do(t, "DELETE", srv.URL+"/v1/jobs/"+id, "tenant-b", nil); code != http.StatusNotFound {
		t.Fatalf("cross-tenant DELETE: expected 404, got %d", code)
	}
	if code, _ := do(t, "GET", srv.URL+"/v1/jobs/"+id, "tenant-b", nil); code != http.StatusNotFound {
		t.Fatalf("cross-tenant GET: expected 404, got %d", code)
	}

	code, _ = do(t, "DELETE", srv.URL+"/v1/jobs/"+id, "tenant-a", nil)
	if code != http.StatusOK {
		t.Fatalf("owner DELETE: expected 200, got %d", code)
	}
	awaitJobStatus(t, srv.URL, "tenant-a", id, "cancelled", 5*time.Second)
}

func TestClientDisconnectCancelsJob(t *testing.T) {
	srv := newTestServer(t, defaultLimits(), 1)

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		req, _ := http.NewRequestWithContext(ctx, "POST",
			srv.URL+"/v1/compute?wait=true&timeout_ms=30000",
			bytes.NewReader([]byte(`{"key":"d","work":900000000,"mem_bytes":1024}`)))
		req.Header.Set("X-Tenant-ID", "tenant-a")
		resp, err := http.DefaultClient.Do(req)
		if err == nil {
			resp.Body.Close()
		}
	}()

	time.Sleep(300 * time.Millisecond) // let the job start
	cancel()                           // client goes away
	<-done

	// The job must be cancelled server-side; verify via stats.
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		_, stats := do(t, "GET", srv.URL+"/v1/stats", "tenant-a", nil)
		st, _ := stats["stats"].(map[string]any)
		if cancelled, _ := st["cancelled"].(float64); cancelled >= 1 {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("job was not cancelled after client disconnect")
}

func TestStatsVisibleOnlyToOwnTenant(t *testing.T) {
	srv := newTestServer(t, defaultLimits(), 1)
	if code, _ := do(t, "POST", srv.URL+"/v1/compute?wait=true", "tenant-a",
		[]byte(`{"key":"k","work":1000,"mem_bytes":0}`)); code != 200 {
		t.Fatalf("compute failed")
	}
	_, statsA := do(t, "GET", srv.URL+"/v1/stats", "tenant-a", nil)
	stA, _ := statsA["stats"].(map[string]any)
	if sub, _ := stA["submitted"].(float64); sub != 1 {
		t.Fatalf("tenant-a submitted=%v, want 1", sub)
	}
	_, statsB := do(t, "GET", srv.URL+"/v1/stats", "tenant-b", nil)
	stB, _ := statsB["stats"].(map[string]any)
	if sub, _ := stB["submitted"].(float64); sub != 0 {
		t.Fatalf("tenant-b must not see tenant-a activity, submitted=%v", sub)
	}
}
