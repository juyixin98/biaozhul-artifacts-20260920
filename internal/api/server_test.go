package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"chsim/internal/ring"
	"chsim/internal/sim"
)

func TestRunEndpoint(t *testing.T) {
	sc := sim.Scenario{
		Seed: 9, Vnodes: 64,
		Nodes:    []ring.Node{{ID: "n1"}, {ID: "n2"}, {ID: "n3"}},
		Network:  sim.NetConfig{BaseDelayMs: 2, JitterMs: 3, DropRate: 0.05, DupRate: 0.03},
		Transfer: sim.TransferConfig{BatchSize: 16, Concurrency: 2, FetchTimeoutMs: 60},
		Clients:  []sim.ClientConfig{{ID: "c1", StartMs: 0, Ops: 80, IntervalMs: 4, Keys: 20, ReadEvery: 4}},
		Ops: []sim.Op{
			{T: 200, Op: "add_node", NodeID: "n4"},
			{T: 220, Op: "pause_migration"},
			{T: 500, Op: "resume_migration"},
		},
	}
	body, _ := json.Marshal(sc)
	req := httptest.NewRequest(http.MethodPost, "/run", bytes.NewReader(body))
	rec := httptest.NewRecorder()
	Handler().ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec.Code, rec.Body.String())
	}
	var res sim.Result
	if err := json.Unmarshal(rec.Body.Bytes(), &res); err != nil {
		t.Fatalf("bad JSON: %v", err)
	}
	if !res.Verification.Pass {
		t.Fatalf("verification failed: %s", rec.Body.String())
	}
	if len(res.Stats.Barriers) != 1 {
		t.Fatalf("want 1 barrier: %s", rec.Body.String())
	}
}

func TestRunEndpointBadInput(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/run", strings.NewReader("{not json"))
	rec := httptest.NewRecorder()
	Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("want 400, got %d", rec.Code)
	}
}

func TestHealthz(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	rec := httptest.NewRecorder()
	Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK || strings.TrimSpace(rec.Body.String()) != "ok" {
		t.Fatalf("healthz: %d %q", rec.Code, rec.Body.String())
	}
}
