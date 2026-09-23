package server

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"dynpool/pool"
)

func newTestServer(t *testing.T) (*Server, *httptest.Server) {
	t.Helper()
	s, err := New(Options{Name: "http", Workers: 2, QueueCapacity: 8})
	if err != nil {
		t.Fatal(err)
	}
	return s, httptest.NewServer(s.Handler())
}

func post(t *testing.T, base, path string, body any) (int, map[string]any) {
	t.Helper()
	b, _ := json.Marshal(body)
	resp, err := http.Post(base+path, "application/json", bytes.NewReader(b))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	if out == nil {
		out = map[string]any{}
	}
	return resp.StatusCode, out
}

func get(t *testing.T, base, path string) (int, map[string]any) {
	t.Helper()
	resp, err := http.Get(base + path)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	if out == nil {
		out = map[string]any{}
	}
	return resp.StatusCode, out
}

func TestHealthAndStats(t *testing.T) {
	_, ts := newTestServer(t)
	defer ts.Close()

	if code, body := get(t, ts.URL, "/healthz"); code != http.StatusOK || body["status"] != "ok" {
		t.Fatalf("health code=%d body=%v", code, body)
	}
	code, stats := get(t, ts.URL, "/v1/pool")
	if code != http.StatusOK {
		t.Fatalf("stats code=%d", code)
	}
	if stats["state"] != "running" || stats["desired_workers"].(float64) != 2 {
		t.Fatalf("stats=%v", stats)
	}
}

func TestSubmitAndFetch(t *testing.T) {
	s, ts := newTestServer(t)
	defer func() { _ = s.Pool().Shutdown(context.Background()) }()

	code, body := post(t, ts.URL, "/v1/tasks", map[string]any{"id": "t1", "sleep_ms": 10})
	if code != http.StatusAccepted {
		t.Fatalf("submit code=%d body=%v", code, body)
	}
	if body["id"] != "t1" {
		t.Fatalf("body=%v", body)
	}
	// Wait for completion by polling the task endpoint.
	deadline := time.Now().Add(2 * time.Second)
	var state string
	for time.Now().Before(deadline) {
		_, snap := get(t, ts.URL, "/v1/tasks/t1")
		state, _ = snap["state"].(string)
		if state == "completed" || state == "failed" {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	if state != "completed" {
		t.Fatalf("state=%s want completed", state)
	}
}

func TestResizeOverHTTP(t *testing.T) {
	s, ts := newTestServer(t)
	defer func() { _ = s.Pool().Shutdown(context.Background()) }()

	code, body := post(t, ts.URL, "/v1/pool/resize", map[string]any{"workers": 6})
	if code != http.StatusOK {
		t.Fatalf("resize code=%d body=%v", code, body)
	}
	if body["desired_workers"].(float64) != 6 {
		t.Fatalf("body=%v", body)
	}
	if !waitStat(s, func(st pool.Stats) bool { return st.ActiveWorkers == 6 }) {
		t.Fatalf("workers did not grow: %+v", s.Pool().Stats())
	}
	// shrink
	if code, body := post(t, ts.URL, "/v1/pool/resize", map[string]any{"workers": 1}); code != http.StatusOK {
		t.Fatalf("shrink code=%d body=%v", code, body)
	}
	if !waitStat(s, func(st pool.Stats) bool { return st.ActiveWorkers == 1 }) {
		t.Fatalf("workers did not shrink: %+v", s.Pool().Stats())
	}
	// invalid
	if code, _ := post(t, ts.URL, "/v1/pool/resize", map[string]any{"workers": -3}); code != http.StatusBadRequest {
		t.Fatalf("bad resize code=%d want 400", code)
	}
}

func TestRejectOverHTTP(t *testing.T) {
	s, err := New(Options{Name: "r", Workers: 1, QueueCapacity: 1})
	if err != nil {
		t.Fatal(err)
	}
	ts := httptest.NewServer(s.Handler())
	defer ts.Close()
	defer func() { _, _ = s.Pool().ShutdownNow(context.Background()) }()

	// Saturate: one running + one queued (use long sleeps then force-cancel).
	post(t, ts.URL, "/v1/tasks", map[string]any{"id": "block", "sleep_ms": 30000})
	post(t, ts.URL, "/v1/tasks", map[string]any{"id": "queued"})
	code, body := post(t, ts.URL, "/v1/tasks", map[string]any{"id": "overflow"})
	if code != http.StatusServiceUnavailable {
		t.Fatalf("overflow code=%d want 503 body=%v", code, body)
	}
	if !strings.Contains(body["error"].(string), "full") {
		t.Fatalf("error=%v", body)
	}
}

func TestShutdownEndpoints(t *testing.T) {
	s, err := New(Options{Name: "sd", Workers: 2, QueueCapacity: 64})
	if err != nil {
		t.Fatal(err)
	}
	ts := httptest.NewServer(s.Handler())
	defer ts.Close()

	// Queue a handful of quick tasks.
	for i := 0; i < 10; i++ {
		code, _ := post(t, ts.URL, "/v1/tasks", map[string]any{"id": fmt.Sprintf("q%d", i)})
		if code != http.StatusAccepted {
			t.Fatalf("submit %d code=%d", i, code)
		}
	}
	// Graceful shutdown drains them all.
	code, body := post(t, ts.URL, "/v1/pool/shutdown?timeout_ms=5000", struct{}{})
	if code != http.StatusOK {
		t.Fatalf("shutdown code=%d body=%v", code, body)
	}
	stats := body
	if stats["state"] != "terminated" {
		t.Fatalf("state=%v", stats["state"])
	}
	// Submitting after shutdown -> 409.
	code, _ = post(t, ts.URL, "/v1/tasks", map[string]any{"id": "late"})
	if code != http.StatusConflict {
		t.Fatalf("late submit code=%d want 409", code)
	}
}

func TestForceShutdownReturnsNotRun(t *testing.T) {
	s, err := New(Options{Name: "force", Workers: 1, QueueCapacity: 32})
	if err != nil {
		t.Fatal(err)
	}
	ts := httptest.NewServer(s.Handler())
	defer ts.Close()

	post(t, ts.URL, "/v1/tasks", map[string]any{"id": "running", "sleep_ms": 30000})
	// wait for it to start
	if !waitStat(s, func(st pool.Stats) bool { return st.RunningNow == 1 }) {
		t.Fatal("task never started")
	}
	for i := 0; i < 5; i++ {
		post(t, ts.URL, "/v1/tasks", map[string]any{"id": fmt.Sprintf("p%d", i)})
	}
	code, body := post(t, ts.URL, "/v1/pool/shutdown?force=1&timeout_ms=5000", struct{}{})
	if code != http.StatusOK {
		t.Fatalf("force code=%d body=%v", code, body)
	}
	notRun, ok := body["not_run"].([]any)
	if !ok || len(notRun) != 5 {
		t.Fatalf("not_run=%v", body["not_run"])
	}
}

func TestEventsRecorded(t *testing.T) {
	s, ts := newTestServer(t)
	defer func() { _ = s.Pool().Shutdown(context.Background()) }()

	post(t, ts.URL, "/v1/tasks", map[string]any{"id": "ev1"})
	if !waitStat(s, func(st pool.Stats) bool { return st.Completed == 1 }) {
		t.Fatal("task did not complete")
	}
	code, body := get(t, ts.URL, "/v1/events")
	if code != http.StatusOK {
		t.Fatalf("events code=%d", code)
	}
	evs, _ := body["events"].([]any)
	if len(evs) == 0 {
		t.Fatal("no events recorded")
	}
}

func waitStat(s *Server, pred func(pool.Stats) bool) bool {
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if pred(s.Pool().Stats()) {
			return true
		}
		time.Sleep(2 * time.Millisecond)
	}
	return false
}
