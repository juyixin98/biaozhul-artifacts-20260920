package api_test

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/example/gangscheduler/internal/api"
	"github.com/example/gangscheduler/internal/scheduler"
)

type testServer struct {
	t       *testing.T
	handler http.Handler
	sched   *scheduler.Scheduler
}

func newTestServer(t *testing.T, ttl, reap time.Duration) *testServer {
	t.Helper()
	s := scheduler.New(scheduler.Config{DefaultTTL: ttl, ReapEvery: reap})
	t.Cleanup(s.Stop)
	return &testServer{t: t, handler: api.NewServer(s).Handler(), sched: s}
}

func (ts *testServer) do(method, path string, body any) (int, map[string]any) {
	ts.t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		raw, _ := json.Marshal(body)
		rdr = bytes.NewReader(raw)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req := httptest.NewRequest(method, path, rdr)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	ts.handler.ServeHTTP(rec, req)
	var out map[string]any
	if rec.Body.Len() > 0 {
		if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
			ts.t.Fatalf("invalid JSON from %s %s: %v\nbody=%s", method, path, err, rec.Body.String())
		}
	}
	return rec.Code, out
}

func (ts *testServer) mustStatus(status, want int, path string, m map[string]any) map[string]any {
	ts.t.Helper()
	if status != want {
		ts.t.Fatalf("%s: status=%d want %d body=%v", path, status, want, m)
	}
	return m
}

// mustDo performs a request and asserts the exact status code.
func (ts *testServer) mustDo(method, path string, body any, want int) map[string]any {
	ts.t.Helper()
	st, out := ts.do(method, path, body)
	return ts.mustStatus(st, want, method+" "+path, out)
}

func mustGet[T any](t *testing.T, m map[string]any, key string) T {
	t.Helper()
	v, ok := m[key]
	if !ok {
		t.Fatalf("missing key %q in %v", key, m)
	}
	cast, ok := v.(T)
	if !ok {
		t.Fatalf("key %q is %T not %T", key, v, *new(T))
	}
	return cast
}

// TestHTTPAcceptance runs the full acceptance script over HTTP:
//
//  1. two gangs compete for overlapping nodes;
//  2. the first reserves; the second waits (no partial hold);
//  3. a planned node is taken OFFLINE between reserve and commit;
//  4. commit with the old plan/versions is rejected (no partial start);
//  5. held slots are released (no leak / no double occupancy);
//  6. node comes back, replan + commit succeeds;
//  7. releasing the running gang promotes the waiter atomically.
func TestHTTPAcceptance(t *testing.T) {
	ts := newTestServer(t, 5*time.Second, 20*time.Millisecond)

	// --- cluster: 2 nodes x 6 slots ---
	for _, n := range []map[string]any{
		{"name": "n1", "capacity": 6, "labels": map[string]string{"zone": "a"}},
		{"name": "n2", "capacity": 6, "labels": map[string]string{"zone": "b"}},
	} {
		st, _ := ts.do(http.MethodPost, "/v1/nodes", n)
		if st != http.StatusCreated {
			t.Fatalf("add node %v: %d", n["name"], st)
		}
	}

	// --- gang A reserves 4+4 (8 of 12 slots) ---
	gangA := map[string]any{
		"id": "A", "ttl_ms": 5000,
		"tasks": []map[string]any{
			{"id": "a1", "slots": 4, "match_labels": map[string]string{"zone": "a"}},
			{"id": "a2", "slots": 4, "match_labels": map[string]string{"zone": "b"}},
		},
	}
	st, body := ts.do(http.MethodPost, "/v1/gangs", gangA)
	ts.mustStatus(st, http.StatusCreated, "submit A", body)
	if mustGet[string](t, body, "status") != scheduler.GangHeld {
		t.Fatalf("A: %v", body["status"])
	}
	resA := mustGet[map[string]any](t, body, "reservation")
	versionsA := mustGet[map[string]any](t, resA, "node_versions")
	if len(versionsA) != 2 {
		t.Fatalf("A plan must cover 2 nodes: %v", versionsA)
	}

	// --- gang B wants the same 4+4: only 4 slots remain -> WAITING, atomic ---
	gangB := map[string]any{
		"id": "B", "ttl_ms": 5000,
		"tasks": []map[string]any{
			{"id": "b1", "slots": 4, "match_labels": map[string]string{"zone": "a"}},
			{"id": "b2", "slots": 4, "match_labels": map[string]string{"zone": "b"}},
		},
	}
	st, body = ts.do(http.MethodPost, "/v1/gangs", gangB)
	ts.mustStatus(st, http.StatusCreated, "submit B", body)
	if mustGet[string](t, body, "status") != scheduler.GangWaiting {
		t.Fatalf("B must WAIT, got %v", body["status"])
	}
	if _, hasRes := body["reservation"]; hasRes && body["reservation"] != nil {
		t.Fatal("B must not hold any node while waiting")
	}

	// --- node n1 is taken OFFLINE before A commits ---
	st, _ = ts.do(http.MethodPost, "/v1/nodes/n1/status", map[string]any{"status": "OFFLINE"})
	if st != http.StatusOK {
		t.Fatalf("offline n1: %d", st)
	}

	// --- A commits the stale plan -> 409 VERSION_MISMATCH, gang FAILED ---
	versions := map[string]any{}
	for k, v := range versionsA {
		versions[k] = v
	}
	st, body = ts.do(http.MethodPost, "/v1/gangs/A/commit", map[string]any{
		"expected_node_versions": versions,
	})
	if st != http.StatusConflict {
		t.Fatalf("commit A expected 409, got %d body=%v", st, body)
	}
	errObj := mustGet[map[string]any](t, body, "error")
	if errObj["code"] != "VERSION_MISMATCH" {
		t.Fatalf("expected VERSION_MISMATCH, got %v", errObj)
	}

	st, body = ts.do(http.MethodGet, "/v1/gangs/A", nil)
	ts.mustStatus(st, http.StatusOK, "get A", body)
	if mustGet[string](t, body, "status") != scheduler.GangFailed {
		t.Fatalf("A must be FAILED, got %v", body["status"])
	}
	if r, ok := body["reservation"]; ok && r != nil {
		t.Fatalf("A reservation must be released: %v", r)
	}

	// --- no leak: n1 reserved=0, n2 reserved=0; nothing running ---
	st, state := ts.do(http.MethodGet, "/v1/state", nil)
	ts.mustStatus(st, http.StatusOK, "state", state)
	for _, nv := range state["nodes"].([]any) {
		n := nv.(map[string]any)
		if n["reserved"].(float64) != 0 || n["running"].(float64) != 0 {
			t.Fatalf("node %v leaked: reserved=%v running=%v",
				n["name"], n["reserved"], n["running"])
		}
	}

	// --- repeat commit is rejected too (no double occupancy) ---
	st, body = ts.do(http.MethodPost, "/v1/gangs/A/commit", map[string]any{
		"expected_node_versions": versions,
	})
	if st != http.StatusConflict {
		t.Fatalf("second commit A expected 409, got %d", st)
	}

	// --- B is still WAITING while n1 is offline (its zone-a task cannot fit) ---
	st, body = ts.do(http.MethodGet, "/v1/gangs/B", nil)
	if mustGet[string](t, body, "status") != scheduler.GangWaiting {
		t.Fatalf("B must still WAIT with n1 offline, got %v", body["status"])
	}

	// --- n1 back ONLINE: replan A, then commit successfully ---
	st, _ = ts.do(http.MethodPost, "/v1/nodes/n1/status", map[string]any{"status": "ONLINE"})
	if st != http.StatusOK {
		t.Fatalf("online n1: %d", st)
	}
	st, body = ts.do(http.MethodPost, "/v1/gangs/A/replan", nil)
	ts.mustStatus(st, http.StatusOK, "replan A", body)
	// FIFO: B is ahead of A in the queue and B fits now (8 free slots), so B
	// gets promoted first; A stays WAITING. That is the designed queue order.
	if got := mustGet[string](t, body, "status"); got != scheduler.GangWaiting {
		t.Fatalf("A must wait behind FIFO head B, got %s", got)
	}
	st, bodyB := ts.do(http.MethodGet, "/v1/gangs/B", nil)
	if mustGet[string](t, bodyB, "status") != scheduler.GangHeld {
		t.Fatalf("B should be promoted HELD, got %v", bodyB["status"])
	}

	// B commits both tasks atomically.
	resB := mustGet[map[string]any](t, bodyB, "reservation")
	st, body = ts.do(http.MethodPost, "/v1/gangs/B/commit", map[string]any{
		"expected_node_versions": resB["node_versions"],
	})
	ts.mustStatus(st, http.StatusOK, "commit B", body)
	running := body["running"].([]any)
	if len(running) != 2 {
		t.Fatalf("B must run BOTH tasks, got %v", running)
	}

	// A still waits (B holds 8, only 4 free).
	st, bodyA := ts.do(http.MethodGet, "/v1/gangs/A", nil)
	if mustGet[string](t, bodyA, "status") != scheduler.GangWaiting {
		t.Fatalf("A waits while B runs, got %v", bodyA["status"])
	}

	// Release B: A is promoted atomically and commits.
	st, _ = ts.do(http.MethodPost, "/v1/gangs/B/release", nil)
	if st != http.StatusOK {
		t.Fatalf("release B: %d", st)
	}
	st, bodyA = ts.do(http.MethodGet, "/v1/gangs/A", nil)
	if mustGet[string](t, bodyA, "status") != scheduler.GangHeld {
		t.Fatalf("A must be promoted HELD after B release, got %v", bodyA["status"])
	}
	resA2 := mustGet[map[string]any](t, bodyA, "reservation")
	if len(mustGet[[]any](t, resA2, "assignments")) != 2 {
		t.Fatal("A plan must contain both tasks")
	}
	st, body = ts.do(http.MethodPost, "/v1/gangs/A/commit", map[string]any{
		"expected_node_versions": resA2["node_versions"],
	})
	ts.mustStatus(st, http.StatusOK, "commit A2", body)
	if mustGet[string](t, body, "status") != scheduler.GangRunning {
		t.Fatalf("A RUNNING expected, got %v", body["status"])
	}

	// Final accounting: A running 4+4, nothing reserved.
	st, state = ts.do(http.MethodGet, "/v1/state", nil)
	totalRunning, totalReserved := 0.0, 0.0
	for _, nv := range state["nodes"].([]any) {
		n := nv.(map[string]any)
		totalRunning += n["running"].(float64)
		totalReserved += n["reserved"].(float64)
	}
	if totalRunning != 8 || totalReserved != 0 {
		t.Fatalf("final accounting running=%v reserved=%v", totalRunning, totalReserved)
	}
}

// TestHTTPTTLExpiryOverHTTP verifies a never-committed reservation is
// reaped and queued waiters are promoted, purely through the HTTP API.
func TestHTTPTTLExpiryOverHTTP(t *testing.T) {
	ts := newTestServer(t, 80*time.Millisecond, 10*time.Millisecond)

	ts.mustDo(http.MethodPost, "/v1/nodes",
		map[string]any{"name": "n1", "capacity": 2}, http.StatusCreated)

	st, body := ts.do(http.MethodPost, "/v1/gangs", map[string]any{
		"id": "A", "ttl_ms": 60,
		"tasks": []map[string]any{{"id": "a", "slots": 2}},
	})
	ts.mustStatus(st, http.StatusCreated, "submit A", body)

	st, body = ts.do(http.MethodPost, "/v1/gangs", map[string]any{
		"id": "B", "ttl_ms": 5000,
		"tasks": []map[string]any{{"id": "b", "slots": 2}},
	})
	ts.mustStatus(st, http.StatusCreated, "submit B", body)
	if body["status"] != scheduler.GangWaiting {
		t.Fatalf("B WAITING expected, got %v", body["status"])
	}

	time.Sleep(200 * time.Millisecond)

	_, body = ts.do(http.MethodGet, "/v1/gangs/A", nil)
	if body["status"] != scheduler.GangFailed {
		t.Fatalf("A must expire FAILED, got %v", body["status"])
	}
	_, body = ts.do(http.MethodGet, "/v1/gangs/B", nil)
	if body["status"] != scheduler.GangHeld {
		t.Fatalf("B must be promoted after A expiry, got %v", body["status"])
	}
}

// TestHTTPDistinctNodesAndLabels checks the anti-affinity + selectors
// through HTTP: gang cannot hold on one node, waits; adding a second node
// promotes it with one task per node.
func TestHTTPDistinctNodesAndLabels(t *testing.T) {
	ts := newTestServer(t, 5*time.Second, 20*time.Millisecond)
	ts.mustDo(http.MethodPost, "/v1/nodes",
		map[string]any{"name": "gpu-1", "capacity": 8,
			"labels": map[string]string{"tier": "gpu", "zone": "a"}},
		http.StatusCreated)

	st, body := ts.do(http.MethodPost, "/v1/gangs", map[string]any{
		"id":             "G",
		"distinct_nodes": true,
		"tasks": []map[string]any{
			{"id": "t1", "slots": 1, "match_labels": map[string]string{"tier": "gpu"}},
			{"id": "t2", "slots": 1, "in_labels": map[string][]string{"zone": {"a", "b"}}},
		},
	})
	ts.mustStatus(st, http.StatusCreated, "submit G", body)
	if body["status"] != scheduler.GangWaiting {
		t.Fatalf("distinct on one node must WAIT, got %v", body["status"])
	}

	ts.mustDo(http.MethodPost, "/v1/nodes",
		map[string]any{"name": "gpu-2", "capacity": 8,
			"labels": map[string]string{"tier": "gpu", "zone": "b"}},
		http.StatusCreated)

	_, body = ts.do(http.MethodGet, "/v1/gangs/G", nil)
	if body["status"] != scheduler.GangHeld {
		t.Fatalf("G must be HELD with two gpu nodes, got %v", body["status"])
	}
	res := body["reservation"].(map[string]any)
	seen := map[string]bool{}
	for _, a := range res["assignments"].([]any) {
		name := a.(map[string]any)["node_name"].(string)
		if seen[name] {
			t.Fatalf("node %s used twice despite distinct_nodes", name)
		}
		seen[name] = true
	}
}

func TestHTTPValidationErrors(t *testing.T) {
	ts := newTestServer(t, time.Second, time.Hour)

	st, body := ts.do(http.MethodPost, "/v1/nodes", map[string]any{"name": "", "capacity": 1})
	if st != http.StatusBadRequest || body["error"].(map[string]any)["code"] != "BAD_REQUEST" {
		t.Fatalf("empty name: %d %v", st, body)
	}
	st, _ = ts.do(http.MethodPost, "/v1/nodes", map[string]any{"name": "n1", "capacity": 4})
	if st != http.StatusCreated {
		t.Fatalf("create: %d", st)
	}
	st, _ = ts.do(http.MethodPost, "/v1/nodes", map[string]any{"name": "n1", "capacity": 4})
	if st != http.StatusConflict {
		t.Fatalf("dup node expected 409, got %d", st)
	}
	st, body = ts.do(http.MethodPost, "/v1/gangs", map[string]any{
		"id": "g", "tasks": []map[string]any{{"id": "t", "slots": 0}},
	})
	if st != http.StatusBadRequest {
		t.Fatalf("zero slots expected 400, got %d %v", st, body)
	}
	st, _ = ts.do(http.MethodGet, "/v1/nodes/missing", nil)
	if st != http.StatusNotFound {
		t.Fatalf("missing node expected 404, got %d", st)
	}
	// Unknown fields rejected.
	rec := httptest.NewRequest(http.MethodPost, "/v1/nodes",
		bytes.NewReader([]byte(`{"name":"x","capacity":1,"bogus":true}`)))
	w := httptest.NewRecorder()
	ts.handler.ServeHTTP(w, rec)
	if w.Code != http.StatusBadRequest {
		t.Fatalf("unknown field expected 400, got %d", w.Code)
	}
}
