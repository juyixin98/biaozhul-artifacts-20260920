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

	"worksteal/commands"
	"worksteal/scheduler"
)

func newTestServer(t *testing.T, workers int) *Server {
	t.Helper()
	srv, err := New(Config{Addr: "127.0.0.1:0", Workers: workers, EventBuffer: 2048})
	if err != nil {
		t.Fatalf("New server: %v", err)
	}
	commands.Register(srv.Exec)
	ts := httptest.NewServer(srv.http.Handler)
	t.Cleanup(ts.Close)
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = srv.Exec.Shutdown(ctx)
	})
	srv.baseURL = ts.URL
	return srv
}

func postJSON(t *testing.T, url string, body any) (int, map[string]any) {
	t.Helper()
	raw, _ := json.Marshal(body)
	res, err := http.Post(url, "application/json", bytes.NewReader(raw))
	if err != nil {
		t.Fatalf("POST %s: %v", url, err)
	}
	defer res.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(res.Body).Decode(&out)
	return res.StatusCode, out
}

func getJSON(t *testing.T, url string) (int, map[string]any) {
	t.Helper()
	res, err := http.Get(url)
	if err != nil {
		t.Fatalf("GET %s: %v", url, err)
	}
	defer res.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(res.Body).Decode(&out)
	return res.StatusCode, out
}

func TestHealthAndStats(t *testing.T) {
	srv := newTestServer(t, 2)
	if code, body := getJSON(t, srv.baseURL+"/healthz"); code != http.StatusOK || body["ok"] != true {
		t.Fatalf("healthz = %d %v", code, body)
	}
	if code, body := getJSON(t, srv.baseURL+"/stats"); code != http.StatusOK {
		t.Fatalf("stats = %d %v", code, body)
	}
}

func TestSubmitInvalidKind(t *testing.T) {
	srv := newTestServer(t, 2)
	code, body := postJSON(t, srv.baseURL+"/tasks", map[string]any{"kind": "nope"})
	if code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404 (body=%v)", code, body)
	}
	if body["ok"] != false {
		t.Fatalf("ok = %v, want false", body["ok"])
	}
}

func TestSubmitBadJSON(t *testing.T) {
	srv := newTestServer(t, 2)
	res, err := http.Post(srv.baseURL+"/tasks", "application/json", strings.NewReader("{bad"))
	if err != nil {
		t.Fatal(err)
	}
	res.Body.Close()
	if res.StatusCode != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", res.StatusCode)
	}
}

func TestSubmitRecurseAndWait(t *testing.T) {
	srv := newTestServer(t, 4)
	// depth=5 binary tree -> 63 nodes.
	code, body := postJSON(t, srv.baseURL+"/tasks", map[string]any{
		"kind":    "recurse",
		"payload": map[string]any{"depth": 5},
	})
	if code != http.StatusAccepted {
		t.Fatalf("submit = %d %v", code, body)
	}
	task := body["task"].(map[string]any)
	id := task["id"].(string)

	deadline := time.Now().Add(5 * time.Second)
	var info map[string]any
	for time.Now().Before(deadline) {
		code, body = getJSON(t, srv.baseURL+"/tasks/"+id)
		if code != http.StatusOK {
			t.Fatalf("get = %d", code)
		}
		info = body["task"].(map[string]any)
		if state, _ := info["state"].(string); scheduler.State(state).IsTerminal() {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	if info["state"] != string(scheduler.StateSucceeded) {
		t.Fatalf("state = %v err=%v", info["state"], info["err"])
	}
	// JSON numbers decode to float64.
	if v, _ := info["value"].(float64); v != 63 {
		t.Fatalf("value = %v, want 63", info["value"])
	}
}

func TestSubmitFanoutChainSleepFail(t *testing.T) {
	srv := newTestServer(t, 3)

	// fanout of 5 noop leaves
	_, body := postJSON(t, srv.baseURL+"/tasks", map[string]any{
		"kind": "fanout", "payload": map[string]any{"count": 5, "child_kind": "noop"},
	})
	id := body["task"].(map[string]any)["id"].(string)
	if state := waitTerminal(t, srv, id); state != scheduler.StateSucceeded {
		t.Fatalf("fanout state = %s", state)
	}

	// chain of 50
	_, body = postJSON(t, srv.baseURL+"/tasks", map[string]any{
		"kind": "chain", "payload": map[string]any{"count": 50},
	})
	id = body["task"].(map[string]any)["id"].(string)
	if state := waitTerminal(t, srv, id); state != scheduler.StateSucceeded {
		t.Fatalf("chain state = %s", state)
	}

	// sleep 10ms
	_, body = postJSON(t, srv.baseURL+"/tasks", map[string]any{
		"kind": "sleep", "payload": map[string]any{"millis": 10},
	})
	id = body["task"].(map[string]any)["id"].(string)
	if state := waitTerminal(t, srv, id); state != scheduler.StateSucceeded {
		t.Fatalf("sleep state = %s", state)
	}

	// fail task
	_, body = postJSON(t, srv.baseURL+"/tasks", map[string]any{
		"kind": "fail", "payload": map[string]any{"message": "nope"},
	})
	id = body["task"].(map[string]any)["id"].(string)
	if state := waitTerminal(t, srv, id); state != scheduler.StateFailed {
		t.Fatalf("fail state = %s", state)
	}

	// panic task
	_, body = postJSON(t, srv.baseURL+"/tasks", map[string]any{
		"kind": "panic", "payload": map[string]any{"message": "boom"},
	})
	id = body["task"].(map[string]any)["id"].(string)
	if state := waitTerminal(t, srv, id); state != scheduler.StatePanicked {
		t.Fatalf("panic state = %s", state)
	}
}

func TestCancelUnknownTask(t *testing.T) {
	srv := newTestServer(t, 2)
	res, err := http.Post(srv.baseURL+"/tasks/does-not-exist/cancel", "application/json", nil)
	if err != nil {
		t.Fatal(err)
	}
	res.Body.Close()
	if res.StatusCode != http.StatusNotFound {
		t.Fatalf("status = %d, want 404", res.StatusCode)
	}
}

func TestEventsEndpoint(t *testing.T) {
	srv := newTestServer(t, 2)
	postJSON(t, srv.baseURL+"/tasks", map[string]any{"kind": "noop"})
	// Drain to ensure events are recorded.
	_ = srv.Exec.WaitIdle(context.Background())

	code, body := getJSON(t, srv.baseURL+"/events")
	if code != http.StatusOK {
		t.Fatalf("events = %d", code)
	}
	events, ok := body["events"].([]any)
	if !ok || len(events) < 2 {
		t.Fatalf("events = %v", body["events"])
	}
	// Events carry strictly increasing seq.
	var last float64
	for _, ev := range events {
		m := ev.(map[string]any)
		seq, _ := m["seq"].(float64)
		if seq <= last {
			t.Fatalf("non-monotonic seq %v <= %v", seq, last)
		}
		last = seq
	}
	if _, ok := body["last_seq"].(float64); !ok {
		t.Fatalf("last_seq missing: %v", body)
	}
}

func TestListTasks(t *testing.T) {
	srv := newTestServer(t, 2)
	for i := 0; i < 3; i++ {
		postJSON(t, srv.baseURL+"/tasks", map[string]any{"kind": "noop"})
	}
	_ = srv.Exec.WaitIdle(context.Background())
	code, body := getJSON(t, srv.baseURL+"/tasks?limit=10")
	if code != http.StatusOK {
		t.Fatalf("list = %d", code)
	}
	tasks, _ := body["tasks"].([]any)
	if len(tasks) != 3 {
		t.Fatalf("tasks len = %d, want 3", len(tasks))
	}
}

func TestShutdownEndpoint(t *testing.T) {
	// Dedicated server (not the shared helper) so we can fully shut it
	// down without affecting other tests.
	srv, err := New(Config{Addr: "127.0.0.1:0", Workers: 1})
	if err != nil {
		t.Fatal(err)
	}
	commands.Register(srv.Exec)
	ts := httptest.NewServer(srv.http.Handler)
	defer ts.Close()

	code, body := postJSON(t, ts.URL+"/shutdown", nil)
	if code != http.StatusAccepted || body["ok"] != true {
		t.Fatalf("shutdown = %d %v", code, body)
	}
	// The executor eventually reports shutting down and drains.
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if srv.Exec.Stats().ShuttingDown {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if !srv.Exec.Stats().ShuttingDown {
		t.Fatal("executor never entered shutting-down state")
	}
	// New work is rejected with 503.
	res, err := http.Post(ts.URL+"/tasks", "application/json",
		strings.NewReader(`{"kind":"noop"}`))
	if err != nil {
		// Listener closed by HTTP shutdown is also an acceptable
		// terminal outcome; otherwise require 503.
		return
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("post after shutdown = %d, want 503 (or closed listener)", res.StatusCode)
	}
}

func waitTerminal(t *testing.T, srv *Server, id string) scheduler.State {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		_, body := getJSON(t, srv.baseURL+"/tasks/"+id)
		task, ok := body["task"].(map[string]any)
		if !ok {
			t.Fatalf("no task in body: %v", body)
		}
		state := scheduler.State(fmt.Sprint(task["state"]))
		if state.IsTerminal() {
			return state
		}
		time.Sleep(3 * time.Millisecond)
	}
	t.Fatalf("task %s did not reach terminal state", id)
	return ""
}
