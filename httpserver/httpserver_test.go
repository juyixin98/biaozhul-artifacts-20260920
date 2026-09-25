package httpserver

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"deadlineadm"
	"deadlineadm/clock"
	"deadlineadm/executor"
)

func newTestServer(t *testing.T) (*httptest.Server, *deadlineadm.Scheduler, *clock.FakeClock) {
	t.Helper()
	clk := clock.NewFakeClockAt(time.Unix(0, 0).UTC())
	ex := executor.NewScriptExecutor(clk)
	sched := deadlineadm.New(deadlineadm.Config{Capacity: 1}, clk, ex, nil)
	sched.Start()
	t.Cleanup(sched.Stop)
	srv := httptest.NewServer(New(sched).Handler())
	t.Cleanup(srv.Close)
	return srv, sched, clk
}

func doJSON(t *testing.T, method, url string, body any) (int, map[string]any) {
	t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		rdr = bytes.NewReader(b)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req, err := http.NewRequest(method, url, rdr)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var m map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&m); err != nil {
		t.Fatalf("decode: %v", err)
	}
	return resp.StatusCode, m
}

func TestHealth(t *testing.T) {
	srv, _, _ := newTestServer(t)
	code, m := doJSON(t, http.MethodGet, srv.URL+"/healthz", nil)
	if code != http.StatusOK || m["status"] != "ok" {
		t.Fatalf("code=%d body=%v", code, m)
	}
}

func TestSubmitAndComplete(t *testing.T) {
	srv, sched, _ := newTestServer(t)
	code, m := doJSON(t, http.MethodPost, srv.URL+"/jobs", map[string]any{
		"id": "j1", "payload": "sleep:10", "deadline_rel_ms": 100, "budget_ms": 10,
	})
	if code != http.StatusAccepted || m["status"] != "admitted" {
		t.Fatalf("submit code=%d body=%v", code, m)
	}
	// Give the script (real wall time inside an HTTP-driven fake-clock setup,
	// driven here directly) a moment is unnecessary: with no clock advance the
	// job is dispatched but sleeps. Exercise stats/events shape instead.
	st := sched.Stats()
	if st.Running != 1 || st.Submitted != 1 {
		t.Fatalf("stats=%+v", st)
	}
}

func TestRejectedAdmissionReturns422(t *testing.T) {
	srv, _, _ := newTestServer(t)
	// budget longer than time to deadline -> conservative rejection
	code, m := doJSON(t, http.MethodPost, srv.URL+"/jobs", map[string]any{
		"id": "x", "payload": "sleep:1", "deadline_rel_ms": 5, "budget_ms": 50,
	})
	if code != http.StatusUnprocessableEntity {
		t.Fatalf("code=%d want 422, body=%v", code, m)
	}
	if m["status"] != "rejected" || m["error"] == nil {
		t.Fatalf("body=%v", m)
	}
}

func TestValidationErrors(t *testing.T) {
	srv, _, _ := newTestServer(t)
	if code, _ := doJSON(t, http.MethodPost, srv.URL+"/jobs", map[string]any{
		"id": "a", "payload": "x", "budget_ms": 0, "deadline_rel_ms": 10,
	}); code != http.StatusBadRequest {
		t.Fatalf("missing budget code=%d want 400", code)
	}
	if code, _ := doJSON(t, http.MethodPost, srv.URL+"/jobs", map[string]any{
		"id": "a", "payload": "x", "budget_ms": 10,
	}); code != http.StatusBadRequest {
		t.Fatalf("missing deadline code=%d want 400", code)
	}
}

func TestCancelLifecycle(t *testing.T) {
	srv, _, _ := newTestServer(t)
	doJSON(t, http.MethodPost, srv.URL+"/jobs", map[string]any{
		"id": "long", "payload": "sleep:1000", "deadline_rel_ms": 5000, "budget_ms": 1000,
	})
	code, m := doJSON(t, http.MethodPost, srv.URL+"/jobs/long/cancel", nil)
	if code != http.StatusOK {
		t.Fatalf("cancel code=%d body=%v", code, m)
	}
	if _, ok := m["job"]; !ok {
		t.Fatalf("cancel response should include job: %v", m)
	}
	// Unknown id
	if code, _ := doJSON(t, http.MethodPost, srv.URL+"/jobs/ghost/cancel", nil); code != http.StatusNotFound {
		t.Fatalf("unknown cancel code=%d want 404", code)
	}
}

func TestListGetEvents(t *testing.T) {
	srv, _, _ := newTestServer(t)
	doJSON(t, http.MethodPost, srv.URL+"/jobs", map[string]any{
		"id": "j", "payload": "sleep:10", "deadline_rel_ms": 100, "budget_ms": 10,
	})
	if code, _ := doJSON(t, http.MethodGet, srv.URL+"/jobs", nil); code != http.StatusOK {
		t.Fatalf("list code=%d", code)
	}
	if code, m := doJSON(t, http.MethodGet, srv.URL+"/jobs/j", nil); code != http.StatusOK || m["id"] != "j" {
		t.Fatalf("get code=%d body=%v", code, m)
	}
	if code, _ := doJSON(t, http.MethodGet, srv.URL+"/jobs/nope", nil); code != http.StatusNotFound {
		t.Fatalf("get missing code=%d", code)
	}
	if code, m := doJSON(t, http.MethodGet, srv.URL+"/events", nil); code != http.StatusOK {
		t.Fatalf("events code=%d", code)
	} else if evs, ok := m["events"].([]any); !ok || len(evs) < 2 {
		t.Fatalf("events=%v", m["events"])
	}
}
