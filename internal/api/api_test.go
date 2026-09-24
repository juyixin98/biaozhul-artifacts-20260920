package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"dagexec/internal/dag"
	"dagexec/internal/scheduler"
	"dagexec/internal/store"
)

func setup(t *testing.T) (*httptest.Server, func()) {
	t.Helper()
	st, err := store.NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatal(err)
	}
	sched := scheduler.New(st, dag.DefaultRegistry(), scheduler.Options{
		DefaultMaxAttempts: 2,
		DefaultMaxParallel: 4,
		RetryBaseDelay:     time.Millisecond,
	})
	if err := sched.Start(); err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(NewServer(sched).Handler())
	return srv, func() {
		srv.Close()
		sched.Shutdown()
	}
}

func doJSON(t *testing.T, method, url string, body interface{}) (int, map[string]interface{}) {
	t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req, _ := http.NewRequest(method, url, rdr)
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]interface{}
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func TestHealthAndTasks(t *testing.T) {
	srv, cleanup := setup(t)
	defer cleanup()

	resp, err := http.Get(srv.URL + "/health")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("health status %d", resp.StatusCode)
	}

	resp2, err := http.Get(srv.URL + "/tasks")
	if err != nil {
		t.Fatal(err)
	}
	defer resp2.Body.Close()
	var info map[string]interface{}
	if err := json.NewDecoder(resp2.Body).Decode(&info); err != nil {
		t.Fatal(err)
	}
	if _, ok := info["tasks"]; !ok {
		t.Fatal("tasks listing missing")
	}
}

func TestSubmitAndGet(t *testing.T) {
	srv, cleanup := setup(t)
	defer cleanup()

	body := map[string]interface{}{
		"name": "hello",
		"nodes": []map[string]interface{}{
			{"id": "a", "task": "noop"},
		},
	}
	status, out := doJSON(t, http.MethodPost, srv.URL+"/dags", body)
	if status != http.StatusCreated {
		t.Fatalf("submit status=%d body=%v", status, out)
	}
	id, _ := out["id"].(string)
	if id == "" {
		t.Fatal("missing id")
	}

	// wait for completion through the blocking endpoint
	resp, err := http.Get(srv.URL + "/dags/" + id + "/wait?timeout=3s")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var fin map[string]interface{}
	if err := json.NewDecoder(resp.Body).Decode(&fin); err != nil {
		t.Fatal(err)
	}
	if fin["status"] != "succeeded" {
		t.Fatalf("status=%v", fin["status"])
	}
}

func TestSubmitCycleIs400(t *testing.T) {
	srv, cleanup := setup(t)
	defer cleanup()
	body := map[string]interface{}{
		"nodes": []map[string]interface{}{
			{"id": "a", "task": "noop", "deps": []string{"b"}},
			{"id": "b", "task": "noop", "deps": []string{"a"}},
		},
	}
	status, out := doJSON(t, http.MethodPost, srv.URL+"/dags", body)
	if status != http.StatusBadRequest {
		t.Fatalf("status=%d body=%v", status, out)
	}
	details, ok := out["details"].([]interface{})
	if !ok || len(details) == 0 {
		t.Fatalf("expected validation details, got %v", out)
	}
}

func TestUnknownTaskIs400(t *testing.T) {
	srv, cleanup := setup(t)
	defer cleanup()
	status, out := doJSON(t, http.MethodPost, srv.URL+"/dags", map[string]interface{}{
		"nodes": []map[string]interface{}{{"id": "a", "task": "shell.exec"}},
	})
	if status != http.StatusBadRequest || !strings.Contains(out["error"].(string), "whitelist") {
		t.Fatalf("status=%d body=%v", status, out)
	}
}

func TestUnknownDagIs404(t *testing.T) {
	srv, cleanup := setup(t)
	defer cleanup()

	resp, err := http.Get(srv.URL + "/dags/nope")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("status=%d", resp.StatusCode)
	}
	status, _ := doJSON(t, http.MethodPost, srv.URL+"/dags/nope/cancel", nil)
	if status != http.StatusNotFound {
		t.Fatalf("cancel status=%d", status)
	}
}

func TestCancelAndRetryFlow(t *testing.T) {
	srv, cleanup := setup(t)
	defer cleanup()

	// Long enough relative to the test delay for cancel to land while A is
	// active, but short enough that the post-retry wait finishes quickly.
	status, out := doJSON(t, http.MethodPost, srv.URL+"/dags", map[string]interface{}{
		"nodes": []map[string]interface{}{
			{"id": "a", "task": "sleep", "params": map[string]interface{}{"ms": float64(500)}},
			{"id": "b", "task": "noop", "deps": []string{"a"}},
		},
	})
	if status != http.StatusCreated {
		t.Fatalf("submit: %d %v", status, out)
	}
	id := out["id"].(string)
	time.Sleep(30 * time.Millisecond)

	status, out = doJSON(t, http.MethodPost, srv.URL+"/dags/"+id+"/cancel", nil)
	if status != http.StatusAccepted {
		t.Fatalf("cancel status=%d", status)
	}
	time.Sleep(50 * time.Millisecond)

	resp, err := http.Get(srv.URL + "/dags/" + id)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var cur map[string]interface{}
	_ = json.NewDecoder(resp.Body).Decode(&cur)
	if cur["status"] != "cancelled" {
		t.Fatalf("status=%v", cur["status"])
	}
	nodes := cur["nodes"].(map[string]interface{})
	b := nodes["b"].(map[string]interface{})
	if b["total_runs"].(float64) != 0 {
		t.Fatalf("downstream ran after cancel: %v", b)
	}

	// Retry a cancelled DAG: both nodes run this time.
	status, _ = doJSON(t, http.MethodPost, srv.URL+"/dags/"+id+"/retry", nil)
	if status != http.StatusAccepted {
		t.Fatalf("retry status=%d", status)
	}
	resp2, err := http.Get(srv.URL + "/dags/" + id + "/wait?timeout=3s")
	if err != nil {
		t.Fatal(err)
	}
	defer resp2.Body.Close()
	var fin map[string]interface{}
	_ = json.NewDecoder(resp2.Body).Decode(&fin)
	if fin["status"] != "succeeded" {
		t.Fatalf("after retry status=%v", fin["status"])
	}
}

func TestMethodNotAllowed(t *testing.T) {
	srv, cleanup := setup(t)
	defer cleanup()
	req, _ := http.NewRequest(http.MethodDelete, srv.URL+"/health", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("status=%d, want 405", resp.StatusCode)
	}
}
