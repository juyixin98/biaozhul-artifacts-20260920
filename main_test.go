package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"pip-sim/simulator"
)

func newTestServer() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/", handleIndex)
	mux.HandleFunc("/healthz", handleHealth)
	mux.HandleFunc("/api/scenarios", handleScenarios)
	mux.HandleFunc("/api/simulate", handleSimulate)
	mux.HandleFunc("/api/compare", handleCompare)
	mux.HandleFunc("/api/demos/", handleDemo)
	return mux
}

func postJSON(t *testing.T, h http.Handler, path string, body any) (int, map[string]any) {
	t.Helper()
	raw, _ := json.Marshal(body)
	req := httptest.NewRequest(http.MethodPost, path, bytes.NewReader(raw))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var out map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &out)
	return rec.Code, out
}

func get(t *testing.T, h http.Handler, path string) (int, map[string]any) {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, path, nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var out map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &out)
	return rec.Code, out
}

func TestHealthAndIndex(t *testing.T) {
	h := newTestServer()
	if code, body := get(t, h, "/healthz"); code != http.StatusOK || body["status"] != "ok" {
		t.Fatalf("healthz code=%d body=%v", code, body)
	}
	code, body := get(t, h, "/")
	if code != http.StatusOK {
		t.Fatalf("index code=%d", code)
	}
	eps, ok := body["endpoints"].([]any)
	if !ok || len(eps) == 0 {
		t.Fatalf("index should list endpoints, got %v", body)
	}
	if code, _ := get(t, h, "/nope"); code != http.StatusNotFound {
		t.Fatalf("unknown path code=%d, want 404", code)
	}
}

func TestMethodNotAllowed(t *testing.T) {
	h := newTestServer()
	req := httptest.NewRequest(http.MethodGet, "/api/simulate", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("GET /api/simulate code=%d, want 405", rec.Code)
	}
}

func TestScenariosEndpoint(t *testing.T) {
	h := newTestServer()
	code, body := get(t, h, "/api/scenarios")
	if code != http.StatusOK {
		t.Fatalf("code=%d body=%v", code, body)
	}
	list, _ := body["scenarios"].([]any)
	if len(list) != 3 {
		t.Fatalf("want 3 built-in scenarios, got %d", len(list))
	}
}

func TestSimulateEndpointClassic(t *testing.T) {
	h := newTestServer()
	sc, ok := simulator.FindScenario("classic")
	if !ok {
		t.Fatal("scenario missing")
	}
	code, body := postJSON(t, h, "/api/simulate", simulator.Config{
		EnableInheritance: true, Tasks: sc.Tasks,
	})
	if code != http.StatusOK {
		t.Fatalf("code=%d body=%v", code, body)
	}
	if body["status"] != "completed" || body["completed"] != true {
		t.Fatalf("unexpected status: %v", body["status"])
	}
	events, _ := body["events"].([]any)
	if len(events) == 0 {
		t.Fatal("expected decision events")
	}
	// Every event must carry time + kind.
	for _, e := range events {
		ev := e.(map[string]any)
		if _, ok := ev["time"]; !ok {
			t.Errorf("event without time: %v", ev)
		}
		if _, ok := ev["kind"]; !ok {
			t.Errorf("event without kind: %v", ev)
		}
	}
}

func TestSimulateRejectsInvalidJSON(t *testing.T) {
	h := newTestServer()
	req := httptest.NewRequest(http.MethodPost, "/api/simulate",
		strings.NewReader(`{"enableInheritance": true, "bogus": 1}`))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("unknown field should 400, got %d: %s", rec.Code, rec.Body.String())
	}
	req = httptest.NewRequest(http.MethodPost, "/api/simulate",
		strings.NewReader(`{"enableInheritance": true, "tasks": []}`))
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("empty tasks should 400, got %d", rec.Code)
	}
}

func TestCompareEndpoint(t *testing.T) {
	h := newTestServer()
	sc, _ := simulator.FindScenario("classic")
	code, body := postJSON(t, h, "/api/compare", simulator.CompareRequest{Tasks: sc.Tasks})
	if code != http.StatusOK {
		t.Fatalf("code=%d body=%v", code, body)
	}
	if body["highestPriorityWaitingTask"] != "H" {
		t.Fatalf("highest waiter = %v", body["highestPriorityWaitingTask"])
	}
	if body["highestWaiterBlockedWithInheritance"] != float64(3) ||
		body["highestWaiterBlockedWithoutInheritance"] != float64(8) {
		t.Fatalf("unexpected blocking summary: %v / %v",
			body["highestWaiterBlockedWithInheritance"],
			body["highestWaiterBlockedWithoutInheritance"])
	}
	wi, _ := body["withInheritance"].(map[string]any)
	wo, _ := body["withoutInheritance"].(map[string]any)
	if wi["enableInheritance"] != true || wo["enableInheritance"] != false {
		t.Fatal("compare runs have wrong inheritance flags")
	}
}

func TestDemoEndpoints(t *testing.T) {
	h := newTestServer()
	if code, _ := get(t, h, "/api/demos/unknown"); code != http.StatusNotFound {
		t.Fatalf("unknown demo code=%d, want 404", code)
	}
	if code, body := get(t, h, "/api/demos/classic"); code != http.StatusOK || body["id"] != "classic" {
		t.Fatalf("get demo code=%d body=%v", code, body)
	}
	code, body := get(t, h, "/api/demos/classic/simulate?inherit=true")
	if code != http.StatusOK || body["enableInheritance"] != true {
		t.Fatalf("demo simulate code=%d body=%v", code, body)
	}
	res, _ := body["result"].(map[string]any)
	if res["status"] != "completed" {
		t.Fatalf("demo simulate status=%v", res["status"])
	}
	code, body = get(t, h, "/api/demos/classic/simulate?inherit=false")
	if code != http.StatusOK || body["enableInheritance"] != false {
		t.Fatalf("demo simulate inherit=false code=%d body=%v", code, body)
	}
	code, body = get(t, h, "/api/demos/classic/compare")
	if code != http.StatusOK {
		t.Fatalf("demo compare code=%d body=%v", code, body)
	}
	if _, ok := body["comparison"]; !ok {
		t.Fatalf("demo compare missing comparison payload: %v", body)
	}
	if code, _ = get(t, h, "/api/demos/deadlock/compare"); code != http.StatusOK {
		t.Fatalf("deadlock compare code=%d", code)
	}
}

func TestDemoDeadlockReports(t *testing.T) {
	h := newTestServer()
	_, body := get(t, h, "/api/demos/deadlock/simulate?inherit=true")
	res, _ := body["result"].(map[string]any)
	if res == nil || res["deadlocked"] != true {
		t.Fatalf("deadlock demo should report deadlocked: %v", res)
	}
}
