package api_test

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"robotdispatch/api"
	"robotdispatch/model"
)

func TestHealthz(t *testing.T) {
	srv := httptest.NewServer(api.NewServer())
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status %d", resp.StatusCode)
	}
	var body map[string]string
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if body["status"] != "ok" {
		t.Fatalf("body = %v", body)
	}
}

func TestRoot(t *testing.T) {
	srv := httptest.NewServer(api.NewServer())
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status %d", resp.StatusCode)
	}
}

func TestPlansMustCharge(t *testing.T) {
	srv := httptest.NewServer(api.NewServer())
	defer srv.Close()

	reqBody := `{
	  "robots": [
	    {"id":"r1","start":{"x":0,"y":0},"battery":6,"batteryCapacity":20,"chargeRate":2,"payloadCapacity":10}
	  ],
	  "tasks": [
	    {"id":"j1","loc":{"x":10,"y":0},"payload":1,"ready":0,"due":100,"service":1}
	  ],
	  "chargers": [
	    {"id":"c1","loc":{"x":5,"y":0}}
	  ]
	}`
	resp, err := http.Post(srv.URL+"/api/v1/plans", "application/json", strings.NewReader(reqBody))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status %d", resp.StatusCode)
	}
	var got model.Response
	if err := json.NewDecoder(resp.Body).Decode(&got); err != nil {
		t.Fatal(err)
	}
	if !got.Feasible {
		t.Fatalf("expected feasible: %s", got.Reason)
	}
	// arrive c1 at t=5 (bat 1), charge to 20 takes ceil(19/2)=10 -> t=15,
	// drive 5 -> arrive task t=20, service 1 -> makespan 21
	if got.Makespan != 21 {
		t.Fatalf("makespan = %d, want 21", got.Makespan)
	}
}

func TestPlansInfeasibleEnergy(t *testing.T) {
	srv := httptest.NewServer(api.NewServer())
	defer srv.Close()

	reqBody := `{
	  "robots": [
	    {"id":"r1","start":{"x":0,"y":0},"battery":6,"batteryCapacity":20,"chargeRate":2,"payloadCapacity":10}
	  ],
	  "tasks": [
	    {"id":"j1","loc":{"x":10,"y":0},"payload":1,"ready":0,"due":100,"service":1}
	  ]
	}`
	resp, err := http.Post(srv.URL+"/api/v1/plans", "application/json", strings.NewReader(reqBody))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status %d", resp.StatusCode)
	}
	var got model.Response
	if err := json.NewDecoder(resp.Body).Decode(&got); err != nil {
		t.Fatal(err)
	}
	if got.Feasible {
		t.Fatal("expected infeasible (no charger, battery insufficient)")
	}
	if !strings.Contains(got.Reason, "infeasible") {
		t.Fatalf("reason should explain infeasibility, got %q", got.Reason)
	}
}

func TestPlansBadJSON(t *testing.T) {
	srv := httptest.NewServer(api.NewServer())
	defer srv.Close()

	resp, err := http.Post(srv.URL+"/api/v1/plans", "application/json", strings.NewReader("{not json"))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status %d, want 400", resp.StatusCode)
	}
}

func TestPlansValidationError(t *testing.T) {
	srv := httptest.NewServer(api.NewServer())
	defer srv.Close()

	// battery above capacity
	reqBody := `{
	  "robots": [
	    {"id":"r1","start":{"x":0,"y":0},"battery":99,"batteryCapacity":10,"chargeRate":2,"payloadCapacity":10}
	  ],
	  "tasks": []
	}`
	resp, err := http.Post(srv.URL+"/api/v1/plans", "application/json", bytes.NewBufferString(reqBody))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status %d, want 400", resp.StatusCode)
	}
	var body map[string]string
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if body["error"] == "" {
		t.Fatal("expected error message")
	}
}

func TestPlansEmptyBody(t *testing.T) {
	srv := httptest.NewServer(api.NewServer())
	defer srv.Close()

	resp, err := http.Post(srv.URL+"/api/v1/plans", "application/json", strings.NewReader(""))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status %d, want 400", resp.StatusCode)
	}
}

func TestMethodNotAllowed(t *testing.T) {
	srv := httptest.NewServer(api.NewServer())
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/api/v1/plans")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("status %d, want 405", resp.StatusCode)
	}
}
