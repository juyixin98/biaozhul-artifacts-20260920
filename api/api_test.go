package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"checkpoint-scheduler/sim"
)

const acceptBody = `{
  "capacity": 2,
  "tasks": [
    {"id":"L","priority":1,"arrival_tick":0,"total_work":12,"checkpoint_every":4,"checkpoint_cost":3,"resource_demand":2},
    {"id":"H","priority":10,"arrival_tick":6,"total_work":8,"checkpoint_every":4,"checkpoint_cost":1,"resource_demand":2}
  ]
}`

func TestSimulateHandlerOK(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/simulate", strings.NewReader(acceptBody))
	rec := httptest.NewRecorder()
	NewHandler().ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body: %s", rec.Code, rec.Body.String())
	}
	var out sim.Result
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if out.Preemptive.Makespan != 33 || out.NonPreemptive.Makespan != 27 {
		t.Errorf("makespans = %d / %d, want 33 / 27", out.Preemptive.Makespan, out.NonPreemptive.Makespan)
	}
	if len(out.TaskComparison) != 2 {
		t.Errorf("task_comparison length = %d, want 2", len(out.TaskComparison))
	}
}

func TestSimulateHandlerErrors(t *testing.T) {
	cases := []struct {
		name string
		body string
	}{
		{"malformed json", `{not json`},
		{"unknown field", `{"capacity":1,"nope":1,"tasks":[{"id":"a","total_work":1,"checkpoint_every":1,"resource_demand":1}]}`},
		{"no tasks", `{"capacity":1,"tasks":[]}`},
		{"invalid task", `{"capacity":1,"tasks":[{"id":"a","total_work":0,"checkpoint_every":1,"resource_demand":1}]}`},
	}
	for _, c := range cases {
		req := httptest.NewRequest(http.MethodPost, "/simulate", strings.NewReader(c.body))
		rec := httptest.NewRecorder()
		NewHandler().ServeHTTP(rec, req)
		if rec.Code != http.StatusBadRequest {
			t.Errorf("%s: status = %d, want 400, body: %s", c.name, rec.Code, rec.Body.String())
		}
		if !strings.Contains(rec.Body.String(), "error") {
			t.Errorf("%s: error response missing error field: %s", c.name, rec.Body.String())
		}
	}
}

func TestHealthAndIndex(t *testing.T) {
	h := NewHandler()

	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("healthz status = %d", rec.Code)
	}

	req = httptest.NewRequest(http.MethodGet, "/", nil)
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("index status = %d", rec.Code)
	}

	req = httptest.NewRequest(http.MethodGet, "/bogus", nil)
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("bogus path status = %d, want 404", rec.Code)
	}
}

// TestBodyTooLarge ensures the body size cap rejects oversized requests.
func TestBodyTooLarge(t *testing.T) {
	big := bytes.Repeat([]byte("a"), maxBodyBytes+10)
	body := append([]byte(`{"capacity":1,"tasks":[{"id":"`), big...)
	body = append(body, []byte(`","total_work":1,"checkpoint_every":1,"resource_demand":1}]}`)...)
	req := httptest.NewRequest(http.MethodPost, "/simulate", bytes.NewReader(body))
	rec := httptest.NewRecorder()
	NewHandler().ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
}
