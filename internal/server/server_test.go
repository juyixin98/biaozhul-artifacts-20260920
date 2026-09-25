package server

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func newTestServer(t *testing.T) (*httptest.Server, *Server) {
	t.Helper()
	s := New(256)
	return httptest.NewServer(s.Handler()), s
}

func doJSON(t *testing.T, h *httptest.Server, method, path string, body any) (int, map[string]any) {
	t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req, _ := http.NewRequest(method, h.URL+path, rdr)
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func waitStatus(t *testing.T, h *httptest.Server, pool string, want map[string]any) map[string]any {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	var last map[string]any
	for time.Now().Before(deadline) {
		code, st := doJSON(t, h, "GET", "/v1/pools/"+pool, nil)
		if code != 200 {
			t.Fatalf("status code %d: %v", code, st)
		}
		last = st
		ok := true
		for k, v := range want {
			if st[k] != v {
				ok = false
			}
		}
		if ok {
			return st
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("pool status never reached %v, last=%v", want, last)
	return nil
}

func TestHTTPLifecycle(t *testing.T) {
	h, _ := newTestServer(t)
	defer h.Close()

	code, st := doJSON(t, h, "POST", "/v1/pools", map[string]any{
		"name": "p1", "workers": 3, "queue_size": 8, "reject_policy": "abort",
	})
	if code != 201 {
		t.Fatalf("create: %d %v", code, st)
	}

	code, st = doJSON(t, h, "POST", "/v1/pools", map[string]any{"name": "p1", "workers": 1})
	if code != 409 {
		t.Fatalf("dup create: %d %v", code, st)
	}

	// Three blocking tasks occupy all workers so that after the shrink both
	// retirees are stuck in their tasks (retiring stays observable).
	for _, id := range []string{"blk1", "blk2", "blk3"} {
		code, st = doJSON(t, h, "POST", "/v1/pools/p1/tasks",
			map[string]any{"type": "block", "id": id, "name": id})
		if code != 202 {
			t.Fatalf("submit %s: %d %v", id, code, st)
		}
	}
	waitStatus(t, h, "p1", map[string]any{"running_tasks": float64(3)})

	// Shrink from 3 to 1 while blocks run.
	code, st = doJSON(t, h, "POST", "/v1/pools/p1/workers", map[string]any{"workers": 1})
	if code != 200 {
		t.Fatalf("resize: %d %v", code, st)
	}
	waitStatus(t, h, "p1", map[string]any{"target_workers": float64(1), "retiring_workers": float64(2)})

	// Release all blockers: queued nothing, pool settles at 1 active.
	for _, id := range []string{"blk1", "blk2", "blk3"} {
		code, st = doJSON(t, h, "POST", fmt.Sprintf("/v1/blocks/p1/release?name=%s", id), nil)
		if code != 200 {
			t.Fatalf("release %s: %d %v", id, code, st)
		}
	}
	waitStatus(t, h, "p1", map[string]any{"active_workers": float64(1), "running_tasks": float64(0)})

	// Graceful shutdown.
	code, st = doJSON(t, h, "DELETE", "/v1/pools/p1", nil)
	if code != 200 {
		t.Fatalf("shutdown: %d %v", code, st)
	}
	if st["status"].(map[string]any)["state"] != "stopped" {
		t.Fatalf("not stopped: %v", st)
	}
}

func TestHTTPGracefulShutdownDrainsQueued(t *testing.T) {
	h, _ := newTestServer(t)
	defer h.Close()
	doJSON(t, h, "POST", "/v1/pools", map[string]any{
		"name": "g", "workers": 1, "queue_size": 16,
	})
	// Blocker.
	doJSON(t, h, "POST", "/v1/pools/g/tasks",
		map[string]any{"type": "block", "id": "b", "name": "b"})
	waitStatus(t, h, "g", map[string]any{"running_tasks": float64(1)})

	// Queue 6 echo tasks.
	for i := 0; i < 6; i++ {
		code, st := doJSON(t, h, "POST", "/v1/pools/g/tasks",
			map[string]any{"type": "echo", "payload": fmt.Sprintf("n%d", i)})
		if code != 202 {
			t.Fatalf("submit echo: %d %v", code, st)
		}
	}
	waitStatus(t, h, "g", map[string]any{"queue_len": float64(6)})

	// Shutdown in a goroutine (it waits on the blocker), then release it.
	errCh := make(chan int, 1)
	go func() {
		req, _ := http.NewRequest("DELETE", h.URL+"/v1/pools/g", nil)
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			errCh <- 0
			return
		}
		resp.Body.Close()
		errCh <- resp.StatusCode
	}()
	// Submissions during shutdown get 409.
	waitStatus(t, h, "g", map[string]any{"state": "shutting_down"})
	code, st := doJSON(t, h, "POST", "/v1/pools/g/tasks", map[string]any{"type": "echo"})
	if code != 409 {
		t.Fatalf("submit during shutdown: %d %v", code, st)
	}
	doJSON(t, h, "POST", "/v1/blocks/g/release?name=b", nil)
	if got := <-errCh; got != 200 {
		t.Fatalf("shutdown code=%d", got)
	}
	waitStatus(t, h, "g", map[string]any{"state": "stopped", "completed_tasks": float64(7)})
}

func TestHTTPForceShutdown(t *testing.T) {
	h, _ := newTestServer(t)
	defer h.Close()
	doJSON(t, h, "POST", "/v1/pools", map[string]any{
		"name": "f", "workers": 2, "queue_size": 16,
	})
	doJSON(t, h, "POST", "/v1/pools/f/tasks",
		map[string]any{"type": "block", "id": "b1", "name": "b1"})
	doJSON(t, h, "POST", "/v1/pools/f/tasks",
		map[string]any{"type": "block", "id": "b2", "name": "b2"})
	waitStatus(t, h, "f", map[string]any{"running_tasks": float64(2)})
	for i := 0; i < 4; i++ {
		doJSON(t, h, "POST", "/v1/pools/f/tasks",
			map[string]any{"type": "sleep", "sleep_ms": 1000})
	}
	waitStatus(t, h, "f", map[string]any{"queue_len": float64(4)})

	code, st := doJSON(t, h, "DELETE", "/v1/pools/f?force=1", nil)
	if code != 200 {
		t.Fatalf("force shutdown: %d %v", code, st)
	}
	dropped, _ := st["dropped"].([]any)
	if len(dropped) != 4 {
		t.Fatalf("dropped=%v, want 4", dropped)
	}
	status := st["status"].(map[string]any)
	if status["state"] != "stopped" {
		t.Fatalf("state=%v", status["state"])
	}
	if status["cancelled_tasks"] != float64(2) {
		t.Fatalf("cancelled=%v", status["cancelled_tasks"])
	}
}

func TestHTTPRejectAbort(t *testing.T) {
	h, _ := newTestServer(t)
	defer h.Close()
	doJSON(t, h, "POST", "/v1/pools", map[string]any{
		"name": "r", "workers": 1, "queue_size": 1, "reject_policy": "abort",
	})
	doJSON(t, h, "POST", "/v1/pools/r/tasks",
		map[string]any{"type": "block", "id": "b", "name": "b"})
	waitStatus(t, h, "r", map[string]any{"running_tasks": float64(1)})
	doJSON(t, h, "POST", "/v1/pools/r/tasks", map[string]any{"type": "sleep", "sleep_ms": 50})
	waitStatus(t, h, "r", map[string]any{"queue_len": float64(1)})
	code, st := doJSON(t, h, "POST", "/v1/pools/r/tasks", map[string]any{"type": "echo"})
	if code != 429 {
		t.Fatalf("expected 429, got %d: %v", code, st)
	}
	if st["rejected"] != true {
		t.Fatalf("rejected flag missing: %v", st)
	}
	doJSON(t, h, "POST", "/v1/blocks/r/release?name=b", nil)
	code, _ = doJSON(t, h, "DELETE", "/v1/pools/r", nil)
	if code != 200 {
		t.Fatalf("shutdown: %d", code)
	}
}

func TestHTTPEvents(t *testing.T) {
	h, _ := newTestServer(t)
	defer h.Close()
	doJSON(t, h, "POST", "/v1/pools", map[string]any{"name": "e", "workers": 1, "queue_size": 4})
	doJSON(t, h, "POST", "/v1/pools/e/tasks",
		map[string]any{"type": "echo", "id": "t1", "payload": "hello"})
	code, st := doJSON(t, h, "DELETE", "/v1/pools/e", nil)
	if code != 200 {
		t.Fatalf("delete: %d %v", code, st)
	}
	code, st = doJSON(t, h, "GET", "/v1/pools/e/events?type=task.completed", nil)
	if code != 200 {
		t.Fatalf("events: %d", code)
	}
	evs := st["events"].([]any)
	if len(evs) != 1 {
		t.Fatalf("completed events=%v", evs)
	}
	ev := evs[0].(map[string]any)
	if ev["task_id"] != "t1" || ev["type"] != "task.completed" {
		t.Fatalf("bad event: %v", ev)
	}
	// Task result endpoint.
	code, st = doJSON(t, h, "GET", "/v1/pools/e/tasks/t1", nil)
	if code != 200 || st["result"] != "hello" {
		t.Fatalf("task result: %d %v", code, st)
	}
}

func TestHTTPUnknownPoolAndTask(t *testing.T) {
	h, _ := newTestServer(t)
	defer h.Close()
	code, _ := doJSON(t, h, "GET", "/v1/pools/nope", nil)
	if code != 404 {
		t.Fatalf("got %d", code)
	}
	doJSON(t, h, "POST", "/v1/pools", map[string]any{"name": "u", "workers": 1, "queue_size": 2})
	code, _ = doJSON(t, h, "GET", "/v1/pools/u/tasks/nope", nil)
	if code != 404 {
		t.Fatalf("got %d", code)
	}
	code, _ = doJSON(t, h, "POST", "/v1/pools/u/tasks", map[string]any{"type": "bogus"})
	if code != 400 {
		t.Fatalf("got %d", code)
	}
	doJSON(t, h, "DELETE", "/v1/pools/u", nil)
}
