package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"

	"raftlab/internal/check"
	"raftlab/internal/sim"
)

func newTestServer(t *testing.T, cfg sim.ClusterConfig) http.Handler {
	t.Helper()
	cl, err := sim.NewCluster(cfg)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(cl.Close)
	srv := &server{cl: cl, cluster: cfg}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/cluster", srv.getCluster)
	mux.HandleFunc("GET /v1/nodes/{id}", srv.getNode)
	mux.HandleFunc("POST /v1/tick", srv.postTick)
	mux.HandleFunc("POST /v1/propose", srv.postPropose)
	mux.HandleFunc("POST /v1/nodes/{id}/stop", srv.postStop)
	mux.HandleFunc("POST /v1/nodes/{id}/start", srv.postStart)
	mux.HandleFunc("POST /v1/nodes/{id}/partition", srv.postPartition)
	mux.HandleFunc("POST /v1/heal", srv.postHeal)
	mux.HandleFunc("POST /v1/stale/release", srv.postReleaseStale)
	mux.HandleFunc("POST /v1/enumerate", srv.postEnumerate)
	mux.HandleFunc("GET /v1/figure8", srv.getFigure8)
	mux.HandleFunc("POST /v1/replay", srv.postReplay)
	return mux
}

func doJSON(t *testing.T, h http.Handler, method, path string, body any, into any) *httptest.ResponseRecorder {
	t.Helper()
	var buf bytes.Buffer
	if body != nil {
		if err := json.NewEncoder(&buf).Encode(body); err != nil {
			t.Fatal(err)
		}
	}
	req := httptest.NewRequest(method, path, &buf)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if into != nil && rec.Code < 500 && rec.Body.Len() > 0 {
		if err := json.Unmarshal(rec.Body.Bytes(), into); err != nil {
			t.Fatalf("decode %s: %v\nbody: %s", path, err, rec.Body.String())
		}
	}
	return rec
}

func TestHTTPElectProposeAndFaults(t *testing.T) {
	cfg := sim.ClusterConfig{
		Size: 3, ElectionMin: 8, ElectionMax: 15, Heartbeat: 4, Latency: 1, Seed: 11,
	}
	h := newTestServer(t, cfg)

	// Elect.
	var ticked map[string]any
	rec := doJSON(t, h, "POST", "/v1/tick", map[string]int{"n": 30}, &ticked)
	if rec.Code != http.StatusOK || ticked["ok"] != true {
		t.Fatalf("tick: code=%d body=%s", rec.Code, rec.Body.String())
	}
	var cluster struct {
		LeaderID int `json:"leaderId"`
	}
	doJSON(t, h, "GET", "/v1/cluster", nil, &cluster)
	if cluster.LeaderID < 1 {
		t.Fatal("no leader elected via HTTP")
	}

	// Propose and commit.
	rec = doJSON(t, h, "POST", "/v1/propose", map[string]string{"command": "SET http yes"}, nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("propose code=%d", rec.Code)
	}
	doJSON(t, h, "POST", "/v1/tick", map[string]int{"n": 8}, nil)
	for id := 1; id <= 3; id++ {
		var node struct {
			KV map[string]string `json:"kv"`
		}
		doJSON(t, h, "GET", "/v1/nodes/"+strconv.Itoa(id), nil, &node)
		if node.KV["http"] != "yes" {
			t.Fatalf("node %d did not apply proposal: %v", id, node.KV)
		}
	}

	// Partition the leader, run long enough for a new-term election, then heal
	// and release stale messages: no violation may surface.
	doJSON(t, h, "POST", "/v1/nodes/"+strconv.Itoa(cluster.LeaderID)+"/partition", nil, nil)
	var after struct {
		Violation *check.Violation `json:"violation"`
	}
	rec = doJSON(t, h, "POST", "/v1/tick", map[string]int{"n": 25}, &after)
	if rec.Code == http.StatusConflict {
		t.Fatalf("invariant violated during partition: %s", after.Violation.Message)
	}
	doJSON(t, h, "POST", "/v1/heal", nil, nil)
	doJSON(t, h, "POST", "/v1/stale/release", nil, nil)
	rec = doJSON(t, h, "POST", "/v1/tick", map[string]int{"n": 15}, &after)
	if rec.Code == http.StatusConflict {
		t.Fatalf("invariant violated after stale release: %s", after.Violation.Message)
	}

	// Stop/start round trip.
	rec = doJSON(t, h, "POST", "/v1/nodes/1/stop", nil, nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("stop code=%d", rec.Code)
	}
	rec = doJSON(t, h, "POST", "/v1/nodes/1/start", nil, nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("start code=%d", rec.Code)
	}
}

func TestHTTPEnumerationCorrectIs200(t *testing.T) {
	h := newTestServer(t, sim.ClusterConfig{
		Size: 3, ElectionMin: 8, ElectionMax: 15, Heartbeat: 4, Latency: 1, Seed: 1,
	})
	var rep check.EnumerationReport
	rec := doJSON(t, h, "POST", "/v1/enumerate",
		map[string]int{"size": 3, "maxDepth": 2, "maxTraces": 300, "ticksPerStep": 4}, &rep)
	if rec.Code != http.StatusOK {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body.String())
	}
	if len(rep.Violations) != 0 {
		t.Fatalf("correct implementation produced %d violations", len(rep.Violations))
	}
}

func TestHTTPFigure8BuggyIs409(t *testing.T) {
	h := newTestServer(t, sim.ClusterConfig{
		Size: 5, ElectionMin: 8, ElectionMax: 15, Heartbeat: 4, Latency: 1, Seed: 1,
	})
	var resp struct {
		OK  bool `json:"ok"`
		Res struct {
			Violation *check.Violation `json:"violation"`
		} `json:"result"`
		Trace []check.Action `json:"trace"`
	}
	rec := doJSON(t, h, "GET", "/v1/figure8?buggy=true", nil, &resp)
	if rec.Code != http.StatusConflict {
		t.Fatalf("want 409, got %d", rec.Code)
	}
	if resp.Res.Violation == nil || resp.Res.Violation.Kind != check.ViolationCommittedPrefix ||
		resp.Res.Violation.Index != 1 {
		t.Fatalf("unexpected violation payload: %s", rec.Body.String())
	}

	// Same trace posted to /v1/replay with buggy=true must reproduce.
	var replay struct {
		OK        bool             `json:"ok"`
		Violation *check.Violation `json:"violation"`
	}
	rec = doJSON(t, h, "POST", "/v1/replay",
		map[string]any{"preset": "figure8", "buggy": true, "actions": resp.Trace}, &replay)
	if rec.Code != http.StatusConflict || replay.OK ||
		replay.Violation == nil || replay.Violation.Kind != check.ViolationCommittedPrefix {
		t.Fatalf("replay did not reproduce: code=%d body=%s", rec.Code, rec.Body.String())
	}
}

func TestHTTPReplayRejectsEmpty(t *testing.T) {
	h := newTestServer(t, sim.ClusterConfig{
		Size: 3, ElectionMin: 8, ElectionMax: 15, Heartbeat: 4, Latency: 1, Seed: 1,
	})
	rec := doJSON(t, h, "POST", "/v1/replay", map[string]any{"actions": []int{}}, nil)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("want 400 for empty actions, got %d", rec.Code)
	}
}
