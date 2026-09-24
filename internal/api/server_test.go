package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"drf-scheduler/internal/scheduler"
)

func testServer() (*Server, http.Handler) {
	srv := NewServer(scheduler.New())
	return srv, srv.Mux()
}

func do(t *testing.T, h http.Handler, method, path, body string) (int, map[string]any) {
	t.Helper()
	var r *http.Request
	if body != "" {
		r = httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Content-Type", "application/json")
	} else {
		r = httptest.NewRequest(method, path, nil)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	var out map[string]any
	if w.Body.Len() > 0 {
		if err := json.Unmarshal(w.Body.Bytes(), &out); err != nil {
			t.Fatalf("response not JSON (%d): %s", w.Code, w.Body.String())
		}
	}
	return w.Code, out
}

func postJSON(t *testing.T, h http.Handler, path string, v any) (int, map[string]any) {
	t.Helper()
	b, _ := json.Marshal(v)
	return do(t, h, http.MethodPost, path, string(b))
}

// TestEndToEndHTTP drives the acceptance scenario through HTTP and checks
// placement, queuing, release and conservation.
func TestEndToEndHTTP(t *testing.T) {
	_, h := testServer()

	if code, body := do(t, h, http.MethodGet, "/health", ""); code != 200 || body["status"] != "ok" {
		t.Fatalf("health: code=%d body=%v", code, body)
	}

	if code, body := postJSON(t, h, "/config", map[string]int64{"cpu": 10, "mem": 10}); code != 200 {
		t.Fatalf("config: code=%d body=%v", code, body)
	}
	if code, _ := postJSON(t, h, "/tenants", map[string]any{"name": "A", "weight": 1}); code != 201 {
		t.Fatalf("create A: code=%d", code)
	}
	if code, _ := postJSON(t, h, "/tenants", map[string]any{"name": "B", "weight": 1}); code != 201 {
		t.Fatalf("create B: code=%d", code)
	}

	submit := func(id, tenant string, cpu, mem int64) string {
		code, body := postJSON(t, h, "/tasks", map[string]any{
			"id": id, "tenant": tenant, "demand": map[string]int64{"cpu": cpu, "mem": mem},
		})
		if code != 202 {
			t.Fatalf("submit %s: code=%d body=%v", id, code, body)
		}
		return body["state"].(string)
	}
	if state := submit("a1", "A", 4, 0); state != "running" {
		t.Fatalf("a1 state=%s", state)
	}
	submit("b1", "B", 0, 4)
	submit("a2", "A", 4, 0)
	submit("b2", "B", 0, 4)
	if state := submit("a3", "A", 4, 0); state != "queued" {
		t.Fatalf("a3 state=%s, want queued", state)
	}
	submit("b3", "B", 0, 4)

	code, state := do(t, h, http.MethodGet, "/state", "")
	if code != 200 {
		t.Fatalf("state: %d", code)
	}
	used := state["used"].(map[string]any)
	if used["cpu"].(float64) != 8 || used["mem"].(float64) != 8 {
		t.Fatalf("used = %v, want 8/8", used)
	}
	if len(state["running"].([]any)) != 4 || len(state["queued"].([]any)) != 2 {
		t.Fatalf("running/queued counts wrong: %v", state)
	}

	// GET task status.
	code, body := do(t, h, http.MethodGet, "/tasks/a3", "")
	if code != 200 || body["state"] != "queued" {
		t.Fatalf("get a3: code=%d body=%v", code, body)
	}

	// DELETE releases and reschedules.
	if code, _ := do(t, h, http.MethodDelete, "/tasks/a1", ""); code != 200 {
		t.Fatalf("release a1: %d", code)
	}
	code, body = do(t, h, http.MethodGet, "/tasks/a3", "")
	if code != 200 || body["state"] != "running" {
		t.Fatalf("a3 after release: code=%d body=%v", code, body)
	}
	if code, _ := do(t, h, http.MethodDelete, "/tasks/b1", ""); code != 200 {
		t.Fatalf("release b1: %d", code)
	}

	// Tenant view.
	code, body = do(t, h, http.MethodGet, "/tenants/A", "")
	if code != 200 || body["name"] != "A" {
		t.Fatalf("get tenant A: code=%d body=%v", code, body)
	}
}

// TestHTTPErrorPaths checks status codes for the documented error cases.
func TestHTTPErrorPaths(t *testing.T) {
	_, h := testServer()

	// Tasks before configuration -> 409.
	code, _ := postJSON(t, h, "/tasks", map[string]any{
		"id": "x", "tenant": "A", "demand": map[string]int64{"cpu": 1, "mem": 1},
	})
	if code != 409 {
		t.Fatalf("submit before config code=%d, want 409", code)
	}

	postJSON(t, h, "/config", map[string]int64{"cpu": 4, "mem": 4})

	// Bad JSON -> 400.
	if code, _ := do(t, h, http.MethodPost, "/tenants", "{not json"); code != 400 {
		t.Fatalf("bad json code=%d, want 400", code)
	}
	// Unknown field -> 400 (DisallowUnknownFields).
	if code, _ := postJSON(t, h, "/tenants", map[string]any{"name": "A", "bogus": 1}); code != 400 {
		t.Fatalf("unknown field code=%d, want 400", code)
	}
	// Zero weight is invalid only when explicitly <= 0? Default applies for
	// omitted weight; explicit negative rejected.
	if code, _ := postJSON(t, h, "/tenants", map[string]any{"name": "bad", "weight": -1}); code != 400 {
		t.Fatalf("negative weight code=%d, want 400", code)
	}
	postJSON(t, h, "/tenants", map[string]any{"name": "A", "weight": 1})
	// Duplicate tenant -> 409.
	if code, _ := postJSON(t, h, "/tenants", map[string]any{"name": "A"}); code != 409 {
		t.Fatalf("duplicate tenant code=%d, want 409", code)
	}
	// Unknown tenant submit -> 404.
	code, _ = postJSON(t, h, "/tasks", map[string]any{
		"id": "t", "tenant": "ghost", "demand": map[string]int64{"cpu": 1, "mem": 1},
	})
	if code != 404 {
		t.Fatalf("unknown tenant code=%d, want 404", code)
	}
	// Demand over capacity -> 400.
	code, _ = postJSON(t, h, "/tasks", map[string]any{
		"id": "big", "tenant": "A", "demand": map[string]int64{"cpu": 9, "mem": 1},
	})
	if code != 400 {
		t.Fatalf("oversize task code=%d, want 400", code)
	}
	// Duplicate task -> 409.
	postJSON(t, h, "/tasks", map[string]any{
		"id": "d", "tenant": "A", "demand": map[string]int64{"cpu": 1, "mem": 1},
	})
	code, _ = postJSON(t, h, "/tasks", map[string]any{
		"id": "d", "tenant": "A", "demand": map[string]int64{"cpu": 1, "mem": 1},
	})
	if code != 409 {
		t.Fatalf("duplicate task code=%d, want 409", code)
	}
	// Missing task -> 404 for GET and DELETE.
	if code, _ := do(t, h, http.MethodGet, "/tasks/nope", ""); code != 404 {
		t.Fatalf("get missing task code=%d, want 404", code)
	}
	if code, _ := do(t, h, http.MethodDelete, "/tasks/nope", ""); code != 404 {
		t.Fatalf("delete missing task code=%d, want 404", code)
	}
	// Missing tenant -> 404.
	if code, _ := do(t, h, http.MethodGet, "/tenants/ghost", ""); code != 404 {
		t.Fatalf("get missing tenant code=%d, want 404", code)
	}
	// Method not allowed -> 405 with Allow header.
	req := httptest.NewRequest(http.MethodPut, "/state", nil)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)
	if w.Code != 405 || w.Header().Get("Allow") == "" {
		t.Fatalf("PUT /state code=%d allow=%q, want 405 with Allow", w.Code, w.Header().Get("Allow"))
	}
	// Reconfigure with active tasks -> 409.
	code, _ = postJSON(t, h, "/config", map[string]int64{"cpu": 100, "mem": 100})
	if code != 409 {
		t.Fatalf("reconfigure while busy code=%d, want 409", code)
	}
	// Reset clears state.
	if code, _ := do(t, h, http.MethodPost, "/reset", ""); code != 200 {
		t.Fatalf("reset code=%d", code)
	}
	code, body := do(t, h, http.MethodGet, "/state", "")
	if code != 200 || body["configured"] != false {
		t.Fatalf("state after reset: code=%d body=%v", code, body)
	}
}

// TestQuotaViaHTTP checks that the quota field is accepted and enforced.
func TestQuotaViaHTTP(t *testing.T) {
	_, h := testServer()
	postJSON(t, h, "/config", map[string]int64{"cpu": 10, "mem": 10})
	postJSON(t, h, "/tenants", map[string]any{
		"name": "q", "weight": 1, "quota": map[string]int64{"cpu": 3, "mem": 0},
	})
	code, _ := postJSON(t, h, "/tasks", map[string]any{
		"id": "q1", "tenant": "q", "demand": map[string]int64{"cpu": 2, "mem": 0},
	})
	if code != 202 {
		t.Fatalf("q1 code=%d", code)
	}
	code, body := postJSON(t, h, "/tasks", map[string]any{
		"id": "q2", "tenant": "q", "demand": map[string]int64{"cpu": 2, "mem": 0},
	})
	if code != 202 || body["state"] != "queued" {
		t.Fatalf("q2 should be accepted-but-queued by quota, code=%d body=%v", code, body)
	}
	// Demand above quota rejected outright.
	code, _ = postJSON(t, h, "/tasks", map[string]any{
		"id": "q3", "tenant": "q", "demand": map[string]int64{"cpu": 4, "mem": 0},
	})
	if code != 400 {
		t.Fatalf("over-quota demand code=%d, want 400", code)
	}
}

// Ensure request bodies are consumed from a bytes reader path too (coverage).
func TestEmptyBodyPost(t *testing.T) {
	_, h := testServer()
	r := httptest.NewRequest(http.MethodPost, "/reset", bytes.NewReader(nil))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != 200 {
		t.Fatalf("reset code=%d", w.Code)
	}
}
