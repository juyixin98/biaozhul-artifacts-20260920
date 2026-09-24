package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func newTestServer(t *testing.T, n, w, r int) (*Server, *httptest.Server) {
	t.Helper()
	c, err := NewCluster(n, w, r)
	if err != nil {
		t.Fatal(err)
	}
	s := NewServer(c)
	ts := httptest.NewServer(s.Mux())
	t.Cleanup(ts.Close)
	return s, ts
}

func postJSON(t *testing.T, ts *httptest.Server, path string, body any) (int, map[string]any) {
	t.Helper()
	return jsonRequest(t, ts, http.MethodPost, path, body)
}

func putJSON(t *testing.T, ts *httptest.Server, path string, body any) (int, map[string]any) {
	t.Helper()
	return jsonRequest(t, ts, http.MethodPut, path, body)
}

func jsonRequest(t *testing.T, ts *httptest.Server, method, path string, body any) (int, map[string]any) {
	t.Helper()
	b, _ := json.Marshal(body)
	req, err := http.NewRequest(method, ts.URL+path, bytes.NewReader(b))
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
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func getJSON(t *testing.T, ts *httptest.Server, path string) (int, map[string]any) {
	t.Helper()
	resp, err := http.Get(ts.URL + path)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func TestHTTPWriteAndRead(t *testing.T) {
	_, ts := newTestServer(t, 3, 2, 2)
	status, body := postJSON(t, ts, "/write", map[string]any{
		"key": "color", "value": "blue", "timeout_ms": 1000,
	})
	if status != http.StatusOK {
		t.Fatalf("write status=%d body=%v", status, body)
	}
	if body["quorum"] != true {
		t.Fatalf("expected quorum=true, got %v", body)
	}

	status, body = getJSON(t, ts, "/read?key=color")
	if status != http.StatusOK {
		t.Fatalf("read status=%d body=%v", status, body)
	}
	if body["value"] != "blue" || body["conflict"] != false {
		t.Fatalf("unexpected read body: %v", body)
	}
}

func TestHTTPWriteTimeoutStatus504(t *testing.T) {
	_, ts := newTestServer(t, 3, 3, 2)
	postJSON(t, ts, "/fault", map[string]any{"replica": 2, "mode": "down"})
	postJSON(t, ts, "/fault", map[string]any{"replica": 1, "mode": "delay", "delay_ms": 500})

	status, body := postJSON(t, ts, "/write", map[string]any{
		"key": "k", "value": "partial", "timeout_ms": 100,
	})
	if status != http.StatusGatewayTimeout {
		t.Fatalf("expected 504, got %d (%v)", status, body)
	}
	if body["quorum"] != false {
		t.Fatalf("expected quorum=false in 504 body: %v", body)
	}

	// R=1 直读副本 0：仍能看到未确认版本，接口如实标记。
	status, body = getJSON(t, ts, "/read?key=k&r=1&targets=0")
	if status != http.StatusOK {
		t.Fatalf("status=%d body=%v", status, body)
	}
	if body["has_uncommitted"] != true {
		t.Fatalf("uncommitted partial write must be flagged: %v", body)
	}
}

func TestHTTPRead503WithoutQuorum(t *testing.T) {
	_, ts := newTestServer(t, 3, 3, 3)
	postJSON(t, ts, "/fault", map[string]any{"replica": 1, "mode": "down"})
	postJSON(t, ts, "/fault", map[string]any{"replica": 2, "mode": "down"})
	postJSON(t, ts, "/write", map[string]any{
		"key": "k", "value": "only-on-0", "timeout_ms": 500,
	})
	status, body := getJSON(t, ts, "/read?key=k&timeout_ms=300")
	if status != http.StatusServiceUnavailable {
		t.Fatalf("expected 503, got %d (%v)", status, body)
	}
	if body["value"] != "only-on-0" {
		t.Fatalf("503 body should still carry partial data: %v", body)
	}
}

func TestHTTPConflictAndResolve(t *testing.T) {
	_, ts := newTestServer(t, 5, 3, 3)
	postJSON(t, ts, "/write", map[string]any{
		"key": "k", "value": "A", "targets": []int{0, 1, 2}, "timeout_ms": 1000,
	})
	postJSON(t, ts, "/write", map[string]any{
		"key": "k", "value": "B", "targets": []int{0, 3, 4}, "timeout_ms": 1000,
	})
	status, body := getJSON(t, ts, "/read?key=k&targets=1,3,4&r=3")
	if status != http.StatusOK || body["conflict"] != true {
		t.Fatalf("expected conflict=true, status=%d body=%v", status, body)
	}
	vers, _ := body["versions"].([]any)
	if len(vers) != 2 {
		t.Fatalf("expected 2 versions, got %v", body["versions"])
	}

	status, body = postJSON(t, ts, "/resolve", map[string]any{
		"key": "k", "value": "A-merged-B", "merge_all": true, "timeout_ms": 1000,
	})
	if status != http.StatusOK || body["quorum"] != true {
		t.Fatalf("resolve failed: status=%d body=%v", status, body)
	}
	status, body = getJSON(t, ts, "/read?key=k&repair=1&full=1")
	if status != http.StatusOK {
		t.Fatalf("post-resolve read status=%d body=%v", status, body)
	}
	if body["conflict"] != false || body["value"] != "A-merged-B" {
		t.Fatalf("conflict not resolved: %v", body)
	}
}

func TestHTTPFaultRecoverAndState(t *testing.T) {
	_, ts := newTestServer(t, 3, 2, 2)
	postJSON(t, ts, "/fault", map[string]any{"replica": 2, "mode": "down"})
	postJSON(t, ts, "/write", map[string]any{
		"key": "k", "value": "v1", "timeout_ms": 1000,
	})
	status, body := postJSON(t, ts, "/recover", map[string]any{"replica": 2})
	if status != http.StatusOK || body["status"] != "recovered" {
		t.Fatalf("recover failed: %d %v", status, body)
	}
	status, body = getJSON(t, ts, "/state")
	if status != http.StatusOK {
		t.Fatalf("state status=%d", status)
	}
	reps, _ := body["replicas"].([]any)
	r2, _ := reps[2].(map[string]any)
	data, _ := r2["data"].(map[string]any)
	if _, ok := data["k"]; !ok {
		t.Fatalf("recovered replica 2 should contain key k: %v", r2)
	}
}

func TestHTTPBadInput(t *testing.T) {
	_, ts := newTestServer(t, 3, 2, 2)
	status, body := postJSON(t, ts, "/write", map[string]any{"value": "nokey"})
	if status != http.StatusBadRequest || !strings.Contains(body["error"].(string), "key") {
		t.Fatalf("expected 400 about key, got %d %v", status, body)
	}
	status, body = postJSON(t, ts, "/fault", map[string]any{"replica": 9, "mode": "up"})
	if status != http.StatusBadRequest {
		t.Fatalf("expected 400 for bad replica id, got %d %v", status, body)
	}
	status, _ = getJSON(t, ts, "/read")
	if status != http.StatusBadRequest {
		t.Fatalf("expected 400 for missing key, got %d", status)
	}
}

func TestHTTPConfig(t *testing.T) {
	_, ts := newTestServer(t, 3, 2, 2)
	status, body := getJSON(t, ts, "/config")
	if status != http.StatusOK || body["n"].(float64) != 3 {
		t.Fatalf("unexpected config: %d %v", status, body)
	}
	status, body = putJSON(t, ts, "/config", map[string]any{"n": 5, "w": 3, "r": 3})
	if status != http.StatusOK || body["replicas_reset"] != true {
		t.Fatalf("config update failed: %d %v", status, body)
	}
	status, body = putJSON(t, ts, "/config", map[string]any{"n": 5, "w": 6, "r": 3})
	if status != http.StatusBadRequest {
		t.Fatalf("expected 400 for W>N, got %d %v", status, body)
	}
}

// 慢副本在协调者超时后晚到落地，验证“晚到”的真实效果可以被观测。
func TestDelayedReplicaEventuallyReceivesIntent(t *testing.T) {
	c, _ := NewCluster(3, 3, 2)
	_ = c.SetFault(1, modeDelay, 150*time.Millisecond)
	_ = c.SetFault(2, modeDown, 0)
	w := c.Write("k", "late", nil, 0, WriteOptions{Timeout: 60 * time.Millisecond})
	if w.Quorum {
		t.Fatalf("expected timeout, got %+v", w)
	}
	time.Sleep(300 * time.Millisecond)
	if got := c.replicas[1].get("k"); len(got) != 1 {
		t.Fatalf("delay replica should eventually hold late intent, got %+v", got)
	}
	if got := c.replicas[1].get("k"); got[0].Committed {
		t.Fatalf("late intent after failed quorum must remain uncommitted")
	}
}
