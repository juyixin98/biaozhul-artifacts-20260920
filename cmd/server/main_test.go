package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func startTestServer(t *testing.T) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(newMux())
	t.Cleanup(srv.Close)
	return srv
}

func getJSON(t *testing.T, url string, into any) int {
	t.Helper()
	resp, err := http.Get(url)
	if err != nil {
		t.Fatalf("GET %s: %v", url, err)
	}
	defer resp.Body.Close()
	if err := json.NewDecoder(resp.Body).Decode(into); err != nil {
		t.Fatalf("decode %s: %v", url, err)
	}
	return resp.StatusCode
}

func TestHealthz(t *testing.T) {
	srv := startTestServer(t)
	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status=%d", resp.StatusCode)
	}
}

func TestSuiteEndpointAllPass(t *testing.T) {
	srv := startTestServer(t)
	var body struct {
		Summary map[string]any `json:"summary"`
		Cases   []struct {
			Case    string   `json:"case"`
			Passed  bool     `json:"passed"`
			Reasons []string `json:"reasons"`
		} `json:"cases"`
	}
	if status := getJSON(t, srv.URL+"/suite", &body); status != http.StatusOK {
		t.Fatalf("status=%d", status)
	}
	if body.Summary["all_passed"] != true {
		for _, c := range body.Cases {
			if !c.Passed {
				t.Errorf("case %s failed: %v", c.Case, c.Reasons)
			}
		}
	}
	if total, _ := body.Summary["total"].(float64); total < 7 {
		t.Fatalf("expected >=7 cases, got %v", total)
	}
}

func TestMeshSuccessPreset(t *testing.T) {
	srv := startTestServer(t)
	var body struct {
		Result map[string]any `json:"result"`
	}
	getJSON(t, srv.URL+"/mesh/success", &body)
	if body.Result["verdict"] != "ok" {
		t.Fatalf("verdict=%v, want ok", body.Result["verdict"])
	}
	if used, _ := body.Result["total_used"].(float64); used != 4 {
		t.Fatalf("total_used=%v, want 4", used)
	}
}

func TestMeshNonRetryablePreset(t *testing.T) {
	srv := startTestServer(t)
	var body struct {
		Result map[string]any `json:"result"`
	}
	getJSON(t, srv.URL+"/mesh/non-retryable", &body)
	if body.Result["verdict"] != "non-retryable" {
		t.Fatalf("verdict=%v, want non-retryable", body.Result["verdict"])
	}
	if used, _ := body.Result["total_used"].(float64); used != 4 {
		t.Fatalf("total_used=%v, want 4 (one chain, never replayed)", used)
	}
}

func TestMeshBudgetCapPresetRespectsMax(t *testing.T) {
	srv := startTestServer(t)
	var body struct {
		Result map[string]any `json:"result"`
	}
	getJSON(t, srv.URL+"/mesh/budget-cap?max=6", &body)
	if used, _ := body.Result["total_used"].(float64); used != 6 {
		t.Fatalf("total_used=%v, want exactly root max 6", used)
	}
	if body.Result["verdict"] != "budget-exhausted" {
		t.Fatalf("verdict=%v, want budget-exhausted", body.Result["verdict"])
	}
}

func TestMeshDeadlinePreset(t *testing.T) {
	srv := startTestServer(t)
	var body struct {
		Result map[string]any `json:"result"`
	}
	getJSON(t, srv.URL+"/mesh/deadline", &body)
	if body.Result["verdict"] != "deadline" {
		t.Fatalf("verdict=%v, want deadline", body.Result["verdict"])
	}
}

func TestMeshUnknownPreset(t *testing.T) {
	srv := startTestServer(t)
	resp, err := http.Get(srv.URL + "/mesh/nope")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("status=%d, want 404", resp.StatusCode)
	}
}

func TestIndex(t *testing.T) {
	srv := startTestServer(t)
	resp, err := http.Get(srv.URL + "/")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status=%d", resp.StatusCode)
	}
}
