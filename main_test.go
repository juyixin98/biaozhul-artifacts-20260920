package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

const scenarioJSON = `{
  "slots": 1,
  "tasks": [
    {"id": "low", "priority": 1, "arrival": 0, "work": 100, "checkpoint_interval": 20, "checkpoint_cost": 5},
    {"id": "high", "priority": 9, "arrival": 47, "work": 30, "checkpoint_interval": 10, "checkpoint_cost": 2}
  ]
}`

func TestHealthz(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	rec := httptest.NewRecorder()
	newMux().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
}

func TestSimulateEndpoint(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/api/simulate", strings.NewReader(scenarioJSON))
	rec := httptest.NewRecorder()
	newMux().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200, body: %s", rec.Code, rec.Body)
	}
	var res SimResult
	if err := json.Unmarshal(rec.Body.Bytes(), &res); err != nil {
		t.Fatal(err)
	}
	if !res.PreemptionEnabled {
		t.Fatal("preemption should default to enabled")
	}
	for _, tr := range res.Tasks {
		if tr.ID == "high" && tr.Completion != 81 {
			t.Fatalf("high completion = %v, want 81", tr.Completion)
		}
	}
}

func TestSimulateEndpointPreemptOff(t *testing.T) {
	body := strings.Replace(scenarioJSON, `"slots": 1,`, `"slots": 1, "preempt": false,`, 1)
	req := httptest.NewRequest(http.MethodPost, "/api/simulate", strings.NewReader(body))
	rec := httptest.NewRecorder()
	newMux().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body: %s", rec.Code, rec.Body)
	}
	var res SimResult
	if err := json.Unmarshal(rec.Body.Bytes(), &res); err != nil {
		t.Fatal(err)
	}
	if res.PreemptionEnabled {
		t.Fatal("preemption should be disabled")
	}
	for _, tr := range res.Tasks {
		if tr.ID == "high" && tr.Completion != 154 {
			t.Fatalf("high completion = %v, want 154", tr.Completion)
		}
	}
}

func TestCompareEndpoint(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/api/compare", strings.NewReader(scenarioJSON))
	rec := httptest.NewRecorder()
	newMux().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body: %s", rec.Code, rec.Body)
	}
	var res CompareResult
	if err := json.Unmarshal(rec.Body.Bytes(), &res); err != nil {
		t.Fatal(err)
	}
	if res.CompletionDelta["high"] != 73 || res.CompletionDelta["low"] != -56 {
		t.Fatalf("unexpected deltas: %v", res.CompletionDelta)
	}
}

func TestBadRequests(t *testing.T) {
	cases := []struct {
		name   string
		method string
		path   string
		body   string
		want   int
	}{
		{"invalid json", http.MethodPost, "/api/simulate", `{not json`, http.StatusBadRequest},
		{"unknown field", http.MethodPost, "/api/simulate", `{"slots":1,"tasks":[],"bogus":1}`, http.StatusBadRequest},
		{"no tasks", http.MethodPost, "/api/simulate", `{"slots":1,"tasks":[]}`, http.StatusBadRequest},
		{"zero slots", http.MethodPost, "/api/simulate", `{"slots":0,"tasks":[{"id":"a","priority":1,"arrival":0,"work":1,"checkpoint_interval":1,"checkpoint_cost":0}]}`, http.StatusBadRequest},
		{"wrong method", http.MethodGet, "/api/simulate", "", http.StatusMethodNotAllowed},
		{"compare invalid", http.MethodPost, "/api/compare", `{"slots":1,"tasks":[]}`, http.StatusBadRequest},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(tc.method, tc.path, strings.NewReader(tc.body))
			rec := httptest.NewRecorder()
			newMux().ServeHTTP(rec, req)
			if rec.Code != tc.want {
				t.Fatalf("status = %d, want %d, body: %s", rec.Code, tc.want, rec.Body)
			}
		})
	}
}
