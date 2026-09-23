package httpapi

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"agingqueue/clock"
	"agingqueue/queue"
)

func newTestServer(t *testing.T, ageInterval time.Duration) (*Server, *clock.Fake) {
	t.Helper()
	clk := clock.NewFake(time.Unix(1_000_000, 0))
	s, err := queue.New(queue.Config{
		Clock:       clk,
		Executor:    instantExec{},
		Sink:        queue.NewMemorySink(0),
		AgeInterval: ageInterval,
		Concurrency: 2,
	})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	t.Cleanup(func() { _ = s.Close() })
	return NewServer(s), clk
}

// newWallServer builds a server on the real clock so async dispatch proceeds
// without manual clock advances.
func newWallServer(t *testing.T) *Server {
	t.Helper()
	s, err := queue.New(queue.Config{
		Executor:    instantExec{},
		Sink:        queue.NewMemorySink(0),
		Concurrency: 2,
	})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	t.Cleanup(func() { _ = s.Close() })
	return NewServer(s)
}

// instantExec succeeds immediately for every type.
type instantExec struct{}

func (instantExec) Execute(_ context.Context, _ *queue.Job) error { return nil }

func doJSON(t *testing.T, h http.Handler, method, path string, body any) (int, map[string]any) {
	if t != nil {
		t.Helper()
	}
	var r *http.Request
	if s, ok := body.(string); ok {
		r = httptest.NewRequest(method, path, strings.NewReader(s))
	} else if body != nil {
		b, _ := json.Marshal(body)
		r = httptest.NewRequest(method, path, bytes.NewReader(b))
	} else {
		r = httptest.NewRequest(method, path, nil)
	}
	r.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, r)
	out := map[string]any{}
	if rec.Body.Len() > 0 {
		_ = json.Unmarshal(rec.Body.Bytes(), &out)
	}
	return rec.Code, out
}

func TestHealth(t *testing.T) {
	srv := newWallServer(t)
	code, body := doJSON(t, srv, http.MethodGet, "/health", nil)
	if code != http.StatusOK || body["status"] != "ok" {
		t.Fatalf("health = %d %v", code, body)
	}
}

func TestSubmitGetListCancel(t *testing.T) {
	srv := newWallServer(t)

	code, body := doJSON(t, srv, http.MethodPost, "/jobs", map[string]any{
		"type": "demo", "priority": 3, "payload": map[string]string{"k": "v"},
	})
	if code != http.StatusCreated {
		t.Fatalf("submit code=%d body=%v", code, body)
	}
	id, _ := body["id"].(string)
	if id == "" {
		t.Fatal("missing generated id")
	}
	// Execution is asynchronous; poll for the terminal state.
	if !waitJob(srv, id, string(queue.Succeeded), time.Second) {
		t.Fatalf("job did not succeed: %v", body)
	}

	code, body = doJSON(t, srv, http.MethodGet, "/jobs/"+id, nil)
	if code != http.StatusOK || body["id"] != id {
		t.Fatalf("get = %d %v", code, body)
	}

	code, body = doJSON(t, srv, http.MethodGet, "/jobs", nil)
	if code != http.StatusOK {
		t.Fatalf("list code=%d", code)
	}
	jobs, _ := body["jobs"].([]any)
	if len(jobs) != 1 {
		t.Fatalf("len(jobs)=%d want 1", len(jobs))
	}
}

func TestSubmitValidation(t *testing.T) {
	srv := newWallServer(t)

	code, _ := doJSON(t, srv, http.MethodPost, "/jobs", map[string]any{"priority": 1})
	if code != http.StatusBadRequest {
		t.Fatalf("missing type code=%d want 400", code)
	}

	code, _ = doJSON(t, srv, http.MethodPost, "/jobs", map[string]any{"type": "x", "priority": 99})
	if code != http.StatusBadRequest {
		t.Fatalf("out-of-range priority code=%d want 400", code)
	}

	code, _ = doJSON(t, srv, http.MethodPost, "/jobs", "{not json")
	if code != http.StatusBadRequest {
		t.Fatalf("bad json code=%d want 400", code)
	}
}

func TestCancelUnknownAndTerminal(t *testing.T) {
	srv := newWallServer(t)

	code, _ := doJSON(t, srv, http.MethodPost, "/jobs/ghost/cancel", nil)
	if code != http.StatusNotFound {
		t.Fatalf("unknown cancel code=%d want 404", code)
	}

	_, body := doJSON(t, srv, http.MethodPost, "/jobs", map[string]any{"type": "x"})
	id := body["id"].(string)
	if !waitJob(srv, id, string(queue.Succeeded), time.Second) {
		t.Fatal("job did not reach a terminal state")
	}

	code, _ = doJSON(t, srv, http.MethodPost, "/jobs/"+id+"/cancel", nil)
	if code != http.StatusConflict {
		t.Fatalf("terminal cancel code=%d want 409", code)
	}
}

func TestStatsAndEvents(t *testing.T) {
	srv := newWallServer(t)
	doJSON(t, srv, http.MethodPost, "/jobs", map[string]any{"type": "x", "priority": 1})

	code, body := doJSON(t, srv, http.MethodGet, "/stats", nil)
	if code != http.StatusOK {
		t.Fatalf("stats code=%d", code)
	}
	if _, ok := body["retainedTotal"]; !ok {
		t.Fatalf("stats missing retainedTotal: %v", body)
	}

	code, body = doJSON(t, srv, http.MethodGet, "/events", nil)
	if code != http.StatusOK {
		t.Fatalf("events code=%d", code)
	}
	// Events are emitted asynchronously; poll until the lifecycle has
	// produced submitted plus at least a terminal event.
	deadline := time.Now().Add(time.Second)
	var evs []any
	for time.Now().Before(deadline) {
		_, body = doJSON(t, srv, http.MethodGet, "/events", nil)
		evs, _ = body["events"].([]any)
		if len(evs) >= 2 {
			break
		}
		time.Sleep(2 * time.Millisecond)
	}
	if len(evs) < 2 {
		t.Fatalf("expected submitted+ events, got %d", len(evs))
	}
}

func TestDurationStringParsing(t *testing.T) {
	srv, clk := newTestServer(t, time.Second)
	// A delayed job with a string duration must be honored.
	rec := httptest.NewRecorder()
	r := httptest.NewRequest(http.MethodPost, "/jobs",
		strings.NewReader(`{"type":"x","priority":1,"delay":"2s"}`))
	r.Header.Set("Content-Type", "application/json")
	srv.ServeHTTP(rec, r)
	if rec.Code != http.StatusCreated {
		t.Fatalf("delayed submit code=%d body=%s", rec.Code, rec.Body.String())
	}
	var j map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &j)
	if j["status"] != string(queue.Queued) {
		t.Fatalf("delayed job status=%v want queued", j["status"])
	}
	// Jump past the delay; the instant executor then completes it.
	clk.Advance(2*time.Second + time.Millisecond)
	id := j["id"].(string)
	if !waitJob(srv, id, string(queue.Succeeded), time.Second) {
		t.Fatal("delayed job did not succeed after clock jump")
	}
}

// waitJob polls GET until the job reaches status, using real wall time (the
// fake clock is advanced separately by the test).
func waitJob(h http.Handler, id, status string, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		code, body := doJSON(nil, h, http.MethodGet, "/jobs/"+id, nil)
		if code == http.StatusOK && body["status"] == status {
			return true
		}
		time.Sleep(2 * time.Millisecond)
	}
	return false
}
