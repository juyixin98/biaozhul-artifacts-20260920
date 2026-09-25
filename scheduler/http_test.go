package scheduler

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func newTestServer(t *testing.T, cap Resources) (*Server, *httptest.Server) {
	t.Helper()
	clock := NewFakeClock(time.Unix(0, 0).UTC())
	s, err := New(cap, WithClock(clock), WithExecutor(NewTimedExecutor(clock)))
	if err != nil {
		t.Fatal(err)
	}
	srv := NewServer(s)
	return srv, httptest.NewServer(srv.Handler())
}

func doJSON(t *testing.T, method, url string, body any) (int, map[string]any) {
	t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		rdr = bytes.NewReader(b)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req, err := http.NewRequest(method, url, rdr)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	return resp.StatusCode, out
}

func TestHTTPEndToEnd(t *testing.T) {
	_, ts := newTestServer(t, Resources{CPU: 4000, Mem: 4096})
	defer ts.Close()

	if code, body := doJSON(t, http.MethodGet, ts.URL+"/healthz", nil); code != http.StatusOK || body["status"] != "ok" {
		t.Fatalf("healthz: %d %v", code, body)
	}

	// Create tenants.
	code, body := doJSON(t, http.MethodPost, ts.URL+"/tenants", map[string]any{"id": "A", "weight": 1})
	if code != http.StatusCreated {
		t.Fatalf("create A: %d %v", code, body)
	}
	if code, body = doJSON(t, http.MethodPost, ts.URL+"/tenants", map[string]any{"id": "A", "weight": 1}); code != http.StatusConflict {
		t.Fatalf("duplicate A: %d %v, want 409", code, body)
	}
	if code, body = doJSON(t, http.MethodPost, ts.URL+"/tenants", map[string]any{"id": "B", "weight": 0}); code != http.StatusBadRequest {
		t.Fatalf("bad weight: %d %v, want 400", code, body)
	}
	if code, _ = doJSON(t, http.MethodPost, ts.URL+"/tenants", map[string]any{"id": "B", "weight": 2}); code != http.StatusCreated {
		t.Fatalf("create B: %d", code)
	}

	// Submit tasks.
	task := map[string]any{
		"id": "a1", "tenant_id": "A",
		"request":  map[string]any{"cpu_milli": 3000, "mem_mib": 1024},
		"duration": "2s",
	}
	code, body = doJSON(t, http.MethodPost, ts.URL+"/tasks", task)
	if code != http.StatusAccepted || body["status"] != "RUNNING" {
		t.Fatalf("submit a1: %d %v", code, body)
	}

	// Unknown field rejected.
	resp, err := http.Post(ts.URL+"/tasks", "application/json",
		bytes.NewBufferString(`{"id":"x","tenant_id":"A","bogus":1,"request":{"cpu_milli":1,"mem_mib":1},"duration":"1s"}`))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("unknown field: %d, want 400", resp.StatusCode)
	}

	// Unknown tenant -> 404.
	task["id"] = "ghost"
	if code, _ = doJSON(t, http.MethodPost, ts.URL+"/tasks", map[string]any{
		"id": "x", "tenant_id": "ghost",
		"request":  map[string]any{"cpu_milli": 1, "mem_mib": 1},
		"duration": "1s",
	}); code != http.StatusNotFound {
		t.Fatalf("unknown tenant submit: %d, want 404", code)
	}

	// Task that cannot fit stays WAITING.
	if code, body = doJSON(t, http.MethodPost, ts.URL+"/tasks", map[string]any{
		"id": "b1", "tenant_id": "B",
		"request":  map[string]any{"cpu_milli": 2000, "mem_mib": 4000},
		"duration": "1s",
	}); code != http.StatusAccepted || body["status"] != "WAITING" {
		t.Fatalf("submit b1: %d %v, want 202 WAITING", code, body)
	}

	// Snapshot must not overcommit.
	resp2, err := http.Get(ts.URL + "/snapshot")
	if err != nil {
		t.Fatal(err)
	}
	var snap ClusterSnapshot
	if err := json.NewDecoder(resp2.Body).Decode(&snap); err != nil {
		t.Fatal(err)
	}
	resp2.Body.Close()
	if snap.Used.CPU > snap.Capacity.CPU || snap.Used.Mem > snap.Capacity.Mem {
		t.Fatalf("overcommit: used %v cap %v", snap.Used, snap.Capacity)
	}
	if snap.Available.CPU != 1000 || snap.Available.Mem != 3072 {
		// capacity is 4000 milliCPU / 4096 MiB; a1 uses 3000/1024
		t.Fatalf("available = %v, want {1000 3072}", snap.Available)
	}

	// GET single task / tenant and 404 paths.
	if resp3, err := http.Get(ts.URL + "/tasks/a1"); err != nil || resp3.StatusCode != http.StatusOK {
		t.Fatalf("get task: %v %v", resp3, err)
	}
	if resp3, _ := http.Get(ts.URL + "/tasks/nope"); resp3.StatusCode != http.StatusNotFound {
		t.Fatalf("missing task: %d, want 404", resp3.StatusCode)
	}
	if resp3, _ := http.Get(ts.URL + "/tenants/A"); resp3.StatusCode != http.StatusOK {
		t.Fatalf("get tenant: %d", resp3.StatusCode)
	}
	if resp3, _ := http.Get(ts.URL + "/tenants/nope"); resp3.StatusCode != http.StatusNotFound {
		t.Fatalf("missing tenant: %d, want 404", resp3.StatusCode)
	}

	// Events endpoint with pagination.
	if resp3, err := http.Get(ts.URL + "/events?after=1&limit=2"); err != nil {
		t.Fatal(err)
	} else {
		var evs []Event
		if err := json.NewDecoder(resp3.Body).Decode(&evs); err != nil {
			t.Fatal(err)
		}
		resp3.Body.Close()
		if len(evs) != 2 || evs[0].EventID != 2 {
			t.Fatalf("events pagination = %+v", evs)
		}
	}
	if resp3, _ := http.Get(ts.URL + "/events?limit=bogus"); resp3.StatusCode != http.StatusBadRequest {
		t.Fatalf("bad limit: %d, want 400", resp3.StatusCode)
	}

	// Method not allowed.
	req, _ := http.NewRequest(http.MethodDelete, ts.URL+"/tasks", nil)
	if resp3, err := http.DefaultClient.Do(req); err != nil || resp3.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("delete tasks: %v %v", resp3, err)
	}
}
