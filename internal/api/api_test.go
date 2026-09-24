package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"taskq/internal/store"
)

type testServer struct {
	dir string
	srv *httptest.Server
	now func() time.Time
}

func newTestServer(t *testing.T, ttl time.Duration) *testServer {
	return newTestServerClock(t, ttl, time.Now)
}

func newTestServerClock(t *testing.T, ttl time.Duration, now func() time.Time) *testServer {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "db")
	st, err := store.Open(dir, store.Options{
		LeaseTTL:     ttl,
		Now:          now,
		CompactEvery: 1_000_000,
	})
	if err != nil {
		t.Fatal(err)
	}
	h := (&Server{Store: st}).NewHandler()
	ts := httptest.NewServer(h)
	t.Cleanup(func() {
		ts.Close()
		_ = st.Close()
	})
	return &testServer{dir: dir, srv: ts, now: now}
}

// client issues JSON calls against the running test server.
type client struct{ base string }

func (c client) call(t testing.TB, method, path string, body any) (int, map[string]any) {
	var buf *bytes.Buffer
	if body != nil {
		b, _ := json.Marshal(body)
		buf = bytes.NewBuffer(b)
	} else {
		buf = &bytes.Buffer{}
	}
	req, err := http.NewRequest(method, c.base+path, buf)
	if err != nil {
		if t == nil {
			panic(err)
		}
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		if t == nil {
			panic(err)
		}
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func taskFrom(t *testing.T, body map[string]any) map[string]any {
	t.Helper()
	tk, ok := body["task"].(map[string]any)
	if !ok {
		t.Fatalf("no task in response: %v", body)
	}
	return tk
}

func TestHTTPLifecycle(t *testing.T) {
	ts := newTestServer(t, time.Hour)
	c := client{base: ts.srv.URL}

	// Health.
	if code, _ := c.call(t, "GET", "/healthz", nil); code != 200 {
		t.Fatalf("health = %d", code)
	}

	// Submit.
	code, body := c.call(t, "POST", "/tasks", map[string]any{"payload": map[string]any{"n": 7}})
	if code != 201 {
		t.Fatalf("submit = %d %v", code, body)
	}
	tk := taskFrom(t, body)
	id := tk["id"].(string)
	if tk["state"].(string) != "PENDING" {
		t.Fatalf("state = %v", tk["state"])
	}

	// Claim.
	code, body = c.call(t, "POST", "/tasks/claim", map[string]any{"worker_id": "w1"})
	if code != 200 {
		t.Fatalf("claim = %d %v", code, body)
	}
	token, ok := body["lease_token"].(string)
	if !ok || token == "" {
		t.Fatalf("no lease token: %v", body)
	}
	if taskFrom(t, body)["attempt"].(float64) != 1 {
		t.Fatal("attempt != 1")
	}

	// Heartbeat with a forged token is rejected.
	code, body = c.call(t, "POST", "/tasks/"+id+"/heartbeat",
		map[string]any{"worker_id": "w1", "lease_token": "forgery"})
	if code != 409 {
		t.Fatalf("forged hb = %d %v", code, body)
	}

	// Heartbeat ok.
	if code, _ = c.call(t, "POST", "/tasks/"+id+"/heartbeat",
		map[string]any{"worker_id": "w1", "lease_token": token}); code != 200 {
		t.Fatalf("heartbeat = %d", code)
	}

	// Complete.
	code, body = c.call(t, "POST", "/tasks/"+id+"/complete",
		map[string]any{"worker_id": "w1", "lease_token": token,
			"result": map[string]any{"answer": 42}})
	if code != 200 {
		t.Fatalf("complete = %d %v", code, body)
	}
	tk = taskFrom(t, body)
	if tk["state"].(string) != "COMPLETED" {
		t.Fatalf("state = %v", tk["state"])
	}

	// Cancel after complete -> 409.
	code, body = c.call(t, "POST", "/tasks/"+id+"/cancel", nil)
	if code != 409 {
		t.Fatalf("cancel after complete = %d %v", code, body)
	}
	// Second complete -> 409 stale state.
	code, _ = c.call(t, "POST", "/tasks/"+id+"/complete",
		map[string]any{"worker_id": "w1", "lease_token": token, "result": map[string]any{}})
	if code != 409 {
		t.Fatalf("double complete = %d", code)
	}
	// Unknown task -> 404.
	if code, _ := c.call(t, "GET", "/tasks/nope", nil); code != 404 {
		t.Fatalf("missing task = %d", code)
	}
}

func TestHTTPTimeoutAndRedispatch(t *testing.T) {
	ts := newTestServer(t, 50*time.Millisecond)
	c := client{base: ts.srv.URL}

	_, body := c.call(t, "POST", "/tasks", map[string]any{"payload": "job"})
	id := taskFrom(t, body)["id"].(string)

	_, body = c.call(t, "POST", "/tasks/claim", map[string]any{"worker_id": "w1"})
	oldToken := body["lease_token"].(string)

	time.Sleep(90 * time.Millisecond) // let the wall-clock lease expire

	// Old result is rejected.
	code, body := c.call(t, "POST", "/tasks/"+id+"/complete",
		map[string]any{"worker_id": "w1", "lease_token": oldToken, "result": "old"})
	if code != 409 {
		t.Fatalf("old complete = %d %v", code, body)
	}

	// Reclaim -> attempt 2; only the new lease can finish.
	_, body = c.call(t, "POST", "/tasks/claim", map[string]any{"worker_id": "w2"})
	if taskFrom(t, body)["attempt"].(float64) != 2 {
		t.Fatalf("reclaim body %v", body)
	}
	newToken := body["lease_token"].(string)
	code, body = c.call(t, "POST", "/tasks/"+id+"/complete",
		map[string]any{"worker_id": "w2", "lease_token": newToken, "result": "new"})
	if code != 200 {
		t.Fatalf("new owner complete = %d %v", code, body)
	}
	if taskFrom(t, body)["state"].(string) != "COMPLETED" {
		t.Fatalf("state = %v", body)
	}
}

// TestHTTPCancelCompleteRace fires cancel and complete concurrently over real
// HTTP and asserts exactly one terminal outcome, with the error code on the
// loser and no observable inconsistency.
func TestHTTPCancelCompleteRace(t *testing.T) {
	ts := newTestServer(t, time.Hour)
	c := client{base: ts.srv.URL}

	_, body := c.call(t, "POST", "/tasks", map[string]any{"payload": "job"})
	id := taskFrom(t, body)["id"].(string)
	_, body = c.call(t, "POST", "/tasks/claim", map[string]any{"worker_id": "w1"})
	token := body["lease_token"].(string)

	start := make(chan struct{})
	var wg sync.WaitGroup
	var cancelCode, completeCode int
	wg.Add(2)
	go func() {
		defer wg.Done()
		<-start
		cancelCode, _ = c.call(nil, "POST", "/tasks/"+id+"/cancel", nil)
	}()
	go func() {
		defer wg.Done()
		<-start
		completeCode, _ = c.call(nil, "POST", "/tasks/"+id+"/complete",
			map[string]any{"worker_id": "w1", "lease_token": token, "result": "done"})
	}()
	close(start)
	wg.Wait()

	if (cancelCode == 200) == (completeCode == 200) {
		t.Fatalf("exactly one must win: cancel=%d complete=%d", cancelCode, completeCode)
	}
	_, body = c.call(t, "GET", "/tasks/"+id, nil)
	state := taskFrom(t, body)["state"].(string)
	if state != "CANCELLED" && state != "COMPLETED" {
		t.Fatalf("state = %s", state)
	}
	if state == "CANCELLED" && completeCode != 409 {
		t.Fatalf("loser complete code = %d, want 409", completeCode)
	}
	if state == "COMPLETED" && cancelCode != 409 {
		t.Fatalf("loser cancel code = %d, want 409", cancelCode)
	}
}

// TestHTTPServerRestartPersists runs a full flow, replaces the HTTP server
// with a brand new one on the same data directory, and checks state.
func TestHTTPServerRestartPersists(t *testing.T) {
	base := time.Date(2026, 9, 24, 12, 0, 0, 0, time.UTC)
	clock := func() time.Time { return base }
	ts := newTestServerClock(t, time.Hour, clock)
	c := client{base: ts.srv.URL}
	_, body := c.call(t, "POST", "/tasks", map[string]any{"payload": "keepme"})
	id := taskFrom(t, body)["id"].(string)
	_, body = c.call(t, "POST", "/tasks/claim", map[string]any{"worker_id": "w1"})
	token := body["lease_token"].(string)
	_, _ = c.call(t, "POST", "/tasks/"+id+"/complete",
		map[string]any{"worker_id": "w1", "lease_token": token, "result": "final"})

	// Simulate process restart with the same fixed clock.
	st2, err := store.Open(ts.dir, store.Options{LeaseTTL: time.Hour,
		Now: clock, CompactEvery: 1_000_000})
	if err != nil {
		t.Fatal(err)
	}
	ts2 := httptest.NewServer((&Server{Store: st2}).NewHandler())
	defer ts2.Close()
	defer st2.Close()

	c2 := client{base: ts2.URL}
	_, body = c2.call(t, "GET", "/tasks/"+id, nil)
	tk := taskFrom(t, body)
	if tk["state"].(string) != "COMPLETED" {
		t.Fatalf("state after restart = %v", tk["state"])
	}
	res, _ := tk["result"].(string)
	if res != "final" {
		t.Fatalf("result after restart = %v", tk["result"])
	}
	// A stale retry/cancel against a terminal task is still rejected.
	if code, _ := c2.call(t, "POST", "/tasks/"+id+"/cancel", nil); code != 409 {
		t.Fatalf("cancel persisted-terminal = %d", code)
	}
}

func TestHTTPBadRequests(t *testing.T) {
	ts := newTestServer(t, time.Hour)
	c := client{base: ts.srv.URL}

	resp, err := http.Post(c.base+"/tasks", "application/json", strings.NewReader("{not json"))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != 400 {
		t.Fatalf("bad json = %d", resp.StatusCode)
	}
	if code, _ := c.call(t, "POST", "/tasks/claim", map[string]any{}); code != 400 {
		t.Fatalf("claim without worker = %d", code)
	}
	// Empty pool -> 404 NO_TASK_AVAILABLE.
	if code, body := c.call(t, "POST", "/tasks/claim", map[string]any{"worker_id": "w"}); code != 404 {
		t.Fatalf("empty claim = %d %v", code, body)
	} else if body["error"].(map[string]any)["code"] != "NO_TASK_AVAILABLE" {
		t.Fatalf("code = %v", body)
	}
	// Wrong method.
	req, _ := http.NewRequest("DELETE", c.base+"/tasks", nil)
	r, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	r.Body.Close()
	if r.StatusCode != 405 {
		t.Fatalf("DELETE /tasks = %d", r.StatusCode)
	}

	// Unknown JSON field is rejected (DisallowUnknownFields).
	resp, err = http.Post(c.base+"/tasks", "application/json",
		strings.NewReader(`{"payload":1,"bogus":2}`))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != 400 {
		t.Fatalf("unknown field = %d, want 400", resp.StatusCode)
	}

	// Oversized body is rejected with 413.
	big := strings.Repeat("a", (1<<20)+1024)
	resp, err = http.Post(c.base+"/tasks", "application/json",
		strings.NewReader(`{"payload":"`+big+`"}`))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != 413 {
		t.Fatalf("oversized body = %d, want 413", resp.StatusCode)
	}
}
