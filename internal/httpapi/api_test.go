package httpapi

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"pim/internal/event"
	"pim/internal/scheduler"
)

func TestHealth(t *testing.T) {
	srv := httptest.NewServer(NewServer().Handler())
	defer srv.Close()

	res, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", res.StatusCode)
	}
	var body map[string]string
	if err := json.NewDecoder(res.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if body["status"] != "ok" {
		t.Errorf("body = %v", body)
	}
}

func TestListScenarios(t *testing.T) {
	srv := httptest.NewServer(NewServer().Handler())
	defer srv.Close()

	res, err := http.Get(srv.URL + "/api/scenarios")
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	var body struct {
		Scenarios []map[string]any `json:"scenarios"`
	}
	if err := json.NewDecoder(res.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if len(body.Scenarios) < 6 {
		t.Fatalf("got %d scenarios, want >= 6", len(body.Scenarios))
	}
}

func TestRunBuiltInScenario(t *testing.T) {
	srv := httptest.NewServer(NewServer().Handler())
	defer srv.Close()

	res, err := http.Get(srv.URL + "/api/scenarios/deadlock-ab-ba")
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", res.StatusCode)
	}
	var out scheduler.Result
	if err := json.NewDecoder(res.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	if out.Deadlock == nil || len(out.Deadlock.Cycle) < 3 {
		t.Fatalf("expected deadlock cycle, got %+v", out.Deadlock)
	}
	var kinds []event.Kind
	for _, e := range out.Events {
		kinds = append(kinds, e.Kind)
	}
	if !containsKind(kinds, event.Deadlock) {
		t.Error("timeline missing deadlock event")
	}
}

func TestScenarioNotFound(t *testing.T) {
	srv := httptest.NewServer(NewServer().Handler())
	defer srv.Close()

	res, err := http.Get(srv.URL + "/api/scenarios/does-not-exist")
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusNotFound {
		t.Errorf("status = %d, want 404", res.StatusCode)
	}
}

func TestSimulateCustomSpec(t *testing.T) {
	body := bytes.NewBufferString(`{
	  "name": "http-custom",
	  "resources": ["R"],
	  "options": {"priorityInheritance": true},
	  "tasks": [
	    {"id": "Lo", "arrival": 0, "priority": 1,
	     "program": {"actions": [{"acquire": "R"}, {"cpu": 2}, {"release": "R"}]}},
	    {"id": "Hi", "arrival": 1, "priority": 5,
	     "program": {"actions": [{"acquire": "R"}, {"cpu": 1}, {"release": "R"}]}}
	  ]
	}`)
	srv := httptest.NewServer(NewServer().Handler())
	defer srv.Close()

	res, err := http.Post(srv.URL+"/api/simulate", "application/json", body)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", res.StatusCode)
	}
	var out scheduler.Result
	if err := json.NewDecoder(res.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	if out.Name != "http-custom" {
		t.Errorf("name = %q", out.Name)
	}
	if got := out.Summary.InversionTicks; got != 0 {
		t.Errorf("inversion ticks = %d, want 0 with PI on", got)
	}
	// Lo must inherit Hi's priority 5.
	var inherited bool
	for _, e := range out.Events {
		if e.Kind == event.PriorityChange && e.Task == "Lo" {
			if v, _ := e.Detail["new"].(float64); v == 5 {
				inherited = true
			}
		}
	}
	if !inherited {
		t.Error("Lo never inherited priority 5")
	}
}

func TestSimulateRejectsInvalidSpec(t *testing.T) {
	srv := httptest.NewServer(NewServer().Handler())
	defer srv.Close()

	// Unknown field rejected thanks to DisallowUnknownFields.
	res, err := http.Post(srv.URL+"/api/simulate", "application/json",
		strings.NewReader(`{"bogus": 1}`))
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusBadRequest {
		t.Errorf("unknown-field status = %d, want 400", res.StatusCode)
	}

	// Semantically invalid: acquire undeclared resource.
	res2, err := http.Post(srv.URL+"/api/simulate", "application/json",
		strings.NewReader(`{"tasks":[{"id":"X","priority":1,
		  "program":{"actions":[{"acquire":"MISSING"}]}}]}`))
	if err != nil {
		t.Fatal(err)
	}
	defer res2.Body.Close()
	if res2.StatusCode != http.StatusBadRequest {
		t.Errorf("invalid-resource status = %d, want 400", res2.StatusCode)
	}
}

func containsKind(xs []event.Kind, k event.Kind) bool {
	for _, x := range xs {
		if x == k {
			return true
		}
	}
	return false
}
