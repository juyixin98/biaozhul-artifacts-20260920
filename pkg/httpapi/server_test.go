package httpapi

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"drfscheduler/pkg/scheduler"
)

func newTestServer(t *testing.T) (*Server, *scheduler.Scheduler, *scheduler.FakeClock) {
	t.Helper()
	clk := scheduler.NewFakeClock(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	sink := scheduler.NewMemorySink(0)
	exec := scheduler.NewSimExecutor(clk)
	s, err := scheduler.New(scheduler.Config{
		Capacity: scheduler.Resources{CPU: 4000, Memory: 4000},
		Clock:    clk,
		Executor: exec,
		Sink:     sink,
	})
	if err != nil {
		t.Fatal(err)
	}
	s.Start()
	t.Cleanup(func() { _ = s.Close() })
	srv := NewServer(s, clk)
	return srv, s, clk
}

func do(t *testing.T, h http.Handler, method, path string, body any) (int, map[string]any) {
	t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req := httptest.NewRequest(method, path, rdr)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var out map[string]any
	if rec.Body.Len() > 0 {
		if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
			t.Fatalf("response not JSON (%d): %s", rec.Code, rec.Body.String())
		}
	}
	return rec.Code, out
}

func newRealtimeServer(t *testing.T) (*Server, *scheduler.Scheduler, scheduler.Clock) {
	t.Helper()
	clk := scheduler.NewRealClock()
	s, err := scheduler.New(scheduler.Config{
		Capacity: scheduler.Resources{CPU: 4000, Memory: 4000},
		Clock:    clk,
		Executor: scheduler.NewSimExecutor(clk),
		Sink:     scheduler.NewMemorySink(0),
	})
	if err != nil {
		t.Fatal(err)
	}
	s.Start()
	t.Cleanup(func() { _ = s.Close() })
	return NewServer(s, nil), s, clk
}

func TestHTTPHealthAndNotFound(t *testing.T) {
	srv, _, _ := newTestServer(t)
	code, body := do(t, srv.Mux, "GET", "/healthz", nil)
	if code != 200 || body["mode"] != "simulation" {
		t.Fatalf("health = %d %v", code, body)
	}
	code, _ = do(t, srv.Mux, "GET", "/v1/state", nil)
	if code != 200 {
		t.Fatalf("state code = %d", code)
	}
}

func TestHTTPSubmitRejectsUnknownTenantAndOversize(t *testing.T) {
	srv, _, _ := newTestServer(t)

	code, body := do(t, srv.Mux, "POST", "/v1/tasks", map[string]any{
		"tenant_id": "ghost", "cpu_millicpu": 1000, "memory_mib": 1000, "duration_ms": 100,
	})
	if code != http.StatusNotFound {
		t.Fatalf("unknown tenant code = %d body=%v", code, body)
	}

	code, _ = do(t, srv.Mux, "PUT", "/v1/tenants/A", map[string]any{"weight": 1})
	if code != 200 {
		t.Fatalf("put tenant code = %d", code)
	}

	code, body = do(t, srv.Mux, "POST", "/v1/tasks", map[string]any{
		"tenant_id": "A", "cpu_millicpu": 5000, "memory_mib": 1000, "duration_ms": 100,
	})
	if code != http.StatusUnprocessableEntity {
		t.Fatalf("oversize code = %d body=%v", code, body)
	}
}

// End-to-end DRF scenario over HTTP: two tenants, a big blocking task, small
// tasks arriving, and clock-advance-driven release. Verifies no overcommit is
// ever reported in the state snapshot.
func TestHTTPSchedulingScenario(t *testing.T) {
	srv, s, _ := newTestServer(t)

	for _, tn := range []string{"A", "B"} {
		if code, _ := do(t, srv.Mux, "PUT", "/v1/tenants/"+tn, map[string]any{"weight": 1}); code != 200 {
			t.Fatalf("tenant %s code = %d", tn, code)
		}
	}

	// big fills the cluster for 100 virtual ms.
	code, body := do(t, srv.Mux, "POST", "/v1/tasks", map[string]any{
		"id": "big", "tenant_id": "A", "cpu_millicpu": 4000, "memory_mib": 4000, "duration_ms": 100,
	})
	if code != http.StatusAccepted || body["state"] != "RUNNING" {
		t.Fatalf("big submit code=%d body=%v", code, body)
	}

	// Two small tasks must wait.
	for _, id := range []string{"s1", "s2"} {
		code, body = do(t, srv.Mux, "POST", "/v1/tasks", map[string]any{
			"id": id, "tenant_id": "B", "cpu_millicpu": 1000, "memory_mib": 1000, "duration_ms": 100,
		})
		if code != http.StatusAccepted || body["state"] != "QUEUED" {
			t.Fatalf("%s submit code=%d body=%v", id, code, body)
		}
	}

	// Snapshot invariant while blocked.
	_, state := do(t, srv.Mux, "GET", "/v1/state", nil)
	inv := state["invariant_check"].(map[string]any)
	if inv["used_fits_capacity"] != true {
		t.Fatalf("overcommit while blocked: %v", inv)
	}
	if state["queued_tasks"].(float64) != 2 {
		t.Fatalf("queued = %v, want 2", state["queued_tasks"])
	}

	// Events are queryable and show waiting records with reasons.
	code, evBody := do(t, srv.Mux, "GET", "/v1/events", nil)
	if code != 200 {
		t.Fatalf("events code = %d", code)
	}
	events := evBody["events"].([]any)
	sawWaiting := false
	for _, e := range events {
		em := e.(map[string]any)
		if em["type"] == "TASK_WAITING" {
			sawWaiting = true
		}
	}
	if !sawWaiting {
		t.Fatal("no TASK_WAITING events recorded")
	}

	// Advance virtual time past the big task; small tasks start.
	code, state = do(t, srv.Mux, "POST", "/v1/clock/advance", map[string]any{"advance_ms": 100})
	if code != 200 {
		t.Fatalf("advance code = %d", code)
	}
	if state["running_tasks"].(float64) != 2 {
		t.Fatalf("running after release = %v, want 2", state["running_tasks"])
	}
	inv = state["invariant_check"].(map[string]any)
	if inv["used_fits_capacity"] != true {
		t.Fatalf("overcommit after release: %v", inv)
	}

	// Run everything to completion.
	_, state = do(t, srv.Mux, "POST", "/v1/clock/advance", map[string]any{"advance_ms": 500})
	if state["running_tasks"].(float64) != 0 || state["queued_tasks"].(float64) != 0 {
		t.Fatalf("not drained: %v/%v", state["running_tasks"], state["queued_tasks"])
	}
	if state["completed_tasks"].(float64) != 3 {
		t.Fatalf("completed = %v, want 3", state["completed_tasks"])
	}

	// Task detail endpoint.
	code, task := do(t, srv.Mux, "GET", "/v1/tasks/big", nil)
	if code != 200 || task["state"] != "COMPLETE" {
		t.Fatalf("big detail = %d %v", code, task)
	}

	// Cancel endpoint on an unknown task is 404.
	code, _ = do(t, srv.Mux, "POST", "/v1/tasks/nope/cancel", map[string]any{"reason": "x"})
	if code != http.StatusNotFound {
		t.Fatalf("cancel unknown code = %d, want 404", code)
	}

	// Malformed JSON is 400.
	req := httptest.NewRequest("POST", "/v1/tasks", strings.NewReader("{not json"))
	rec := httptest.NewRecorder()
	srv.Mux.ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("malformed json code = %d", rec.Code)
	}

	// Advancing the clock is rejected in realtime mode.
	realSrv, _, _ := newRealtimeServer(t)
	code, _ = do(t, realSrv.Mux, "POST", "/v1/clock/advance", map[string]any{"advance_ms": 10})
	if code != http.StatusBadRequest {
		t.Fatalf("real-mode advance code = %d, want 400", code)
	}
	_ = s
}

func TestHTTPBatchSubmissionIsSimultaneous(t *testing.T) {
	srv, s, _ := newTestServer(t)
	for _, tn := range []string{"A", "B"} {
		if code, _ := do(t, srv.Mux, "PUT", "/v1/tenants/"+tn, map[string]any{"weight": 1}); code != 200 {
			t.Fatalf("tenant %s code = %d", tn, code)
		}
	}
	// Capacity 9/18 (milli) classic workload as one batch.
	_, _ = do(t, srv.Mux, "PUT", "/v1/cluster/capacity",
		map[string]any{"cpu_millicpu": 9000, "memory_mib": 18000})
	batch := []map[string]any{
		{"id": "A1", "tenant_id": "A", "cpu_millicpu": 1000, "memory_mib": 4000, "duration_ms": 100},
		{"id": "A2", "tenant_id": "A", "cpu_millicpu": 1000, "memory_mib": 4000, "duration_ms": 100},
		{"id": "A3", "tenant_id": "A", "cpu_millicpu": 1000, "memory_mib": 4000, "duration_ms": 100},
		{"id": "A4", "tenant_id": "A", "cpu_millicpu": 1000, "memory_mib": 4000, "duration_ms": 100},
		{"id": "B1", "tenant_id": "B", "cpu_millicpu": 3000, "memory_mib": 1000, "duration_ms": 200},
		{"id": "B2", "tenant_id": "B", "cpu_millicpu": 3000, "memory_mib": 1000, "duration_ms": 50},
		{"id": "B3", "tenant_id": "B", "cpu_millicpu": 3000, "memory_mib": 1000, "duration_ms": 200},
	}
	code, body := do(t, srv.Mux, "POST", "/v1/tasks/batch", batch)
	if code != http.StatusAccepted {
		t.Fatalf("batch code = %d body=%v", code, body)
	}
	if body["count"].(float64) != 7 {
		t.Fatalf("batch count = %v", body["count"])
	}
	// Hand-computed simultaneous-arrival order: A1 B1 A2 B2 A3 running.
	states := map[string]string{}
	for _, tv := range body["tasks"].([]any) {
		m := tv.(map[string]any)
		states[m["id"].(string)] = m["state"].(string)
	}
	for _, id := range []string{"A1", "B1", "A2", "B2", "A3"} {
		if states[id] != "RUNNING" {
			t.Fatalf("%s = %s, want RUNNING (states=%v)", id, states[id], states)
		}
	}
	for _, id := range []string{"A4", "B3"} {
		if states[id] != "QUEUED" {
			t.Fatalf("%s = %s, want QUEUED", id, states[id])
		}
	}
	// A bad element rolls the whole batch back: nothing new is admitted.
	bad := append(batch, map[string]any{
		"id": "X", "tenant_id": "A", "cpu_millicpu": 999999, "memory_mib": 1, "duration_ms": 1,
	})
	if code, _ := do(t, srv.Mux, "POST", "/v1/tasks/batch", bad); code != http.StatusUnprocessableEntity {
		t.Fatalf("oversized batch code = %d, want 422", code)
	}
	if _, err := s.GetTask("A1"); err != nil {
		t.Fatalf("original batch lost after failed second batch: %v", err)
	}
	if _, err := s.GetTask("X"); err == nil {
		t.Fatal("oversized task X was admitted despite batch rejection")
	}
}

func TestHTTPCapacityShrinkBelowUseRejected(t *testing.T) {
	srv, _, _ := newTestServer(t)
	_, _ = do(t, srv.Mux, "PUT", "/v1/tenants/A", map[string]any{"weight": 1})
	do(t, srv.Mux, "POST", "/v1/tasks", map[string]any{
		"id": "big", "tenant_id": "A", "cpu_millicpu": 4000, "memory_mib": 4000, "duration_ms": 1000,
	})
	code, body := do(t, srv.Mux, "PUT", "/v1/cluster/capacity", map[string]any{
		"cpu_millicpu": 1000, "memory_mib": 1000,
	})
	if code != http.StatusUnprocessableEntity {
		t.Fatalf("shrink code = %d body=%v", code, body)
	}
	// Growing capacity works.
	code, _ = do(t, srv.Mux, "PUT", "/v1/cluster/capacity", map[string]any{
		"cpu_millicpu": 8000, "memory_mib": 8000,
	})
	if code != 200 {
		t.Fatalf("grow code = %d", code)
	}
}
