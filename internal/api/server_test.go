package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"autoscaler/internal/scaler"
)

func newTestServer() (*Server, *httptest.Server) {
	s := scaler.New(scaler.DefaultConfig(), 2)
	srv := NewServer(s)
	return srv, httptest.NewServer(srv.Handler())
}

func TestHTTPFlow(t *testing.T) {
	_, ts := newTestServer()
	defer ts.Close()

	// healthz
	resp, err := http.Get(ts.URL + "/healthz")
	if err != nil || resp.StatusCode != 200 {
		t.Fatalf("healthz: %v %v", resp.StatusCode, err)
	}
	resp.Body.Close()

	// ingest metrics
	body := `{"samples":[
		{"time":"2026-09-24T12:00:00Z","pod":"p1","pod_start":"2026-09-24T11:00:00Z","value":200},
		{"time":"2026-09-24T12:00:00Z","pod":"p2","pod_start":"2026-09-24T11:00:00Z","value":200}
	]}`
	resp, err = http.Post(ts.URL+"/v1/metrics", "application/json", bytes.NewBufferString(body))
	if err != nil || resp.StatusCode != 200 {
		t.Fatalf("metrics: %v %v", resp.StatusCode, err)
	}
	resp.Body.Close()

	// evaluate at an explicit time
	resp, err = http.Post(ts.URL+"/v1/evaluate", "application/json",
		bytes.NewBufferString(`{"time":"2026-09-24T12:00:00Z"}`))
	if err != nil || resp.StatusCode != 200 {
		t.Fatalf("evaluate: %v %v", resp.StatusCode, err)
	}
	var d scaler.Decision
	if err := json.NewDecoder(resp.Body).Decode(&d); err != nil {
		t.Fatalf("decode decision: %v", err)
	}
	resp.Body.Close()
	if d.Action != scaler.ActionScaleUp || d.DesiredReplicas != 4 {
		t.Fatalf("got %s to %d, want scale-up to 4", d.Action, d.DesiredReplicas)
	}

	// state reflects the decision
	resp, _ = http.Get(ts.URL + "/v1/state")
	var st scaler.State
	json.NewDecoder(resp.Body).Decode(&st)
	resp.Body.Close()
	if st.Replicas != 4 {
		t.Fatalf("state replicas = %d, want 4", st.Replicas)
	}

	// decisions history has one entry
	resp, _ = http.Get(ts.URL + "/v1/decisions")
	var hist struct {
		Decisions []scaler.Decision `json:"decisions"`
	}
	json.NewDecoder(resp.Body).Decode(&hist)
	resp.Body.Close()
	if len(hist.Decisions) != 1 {
		t.Fatalf("history = %d, want 1", len(hist.Decisions))
	}

	// reset
	resp, err = http.Post(ts.URL+"/v1/reset", "application/json",
		bytes.NewBufferString(`{"replicas":2}`))
	if err != nil || resp.StatusCode != 200 {
		t.Fatalf("reset: %v %v", resp.StatusCode, err)
	}
	resp.Body.Close()

	// bad config rejected
	resp, _ = http.Post(ts.URL+"/v1/evaluate", "application/json",
		bytes.NewBufferString(`{"time":"not-a-time"}`))
	if resp.StatusCode != 400 {
		t.Fatalf("bad time: got %d, want 400", resp.StatusCode)
	}
	resp.Body.Close()
}
