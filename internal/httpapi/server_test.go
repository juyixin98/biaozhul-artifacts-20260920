package httpapi

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"scaler/internal/clock"
	"scaler/internal/crypto"
	"scaler/internal/store"
)

type testHarness struct {
	t      *testing.T
	srv    *httptest.Server
	client *http.Client
	base   string
}

func newHarness(t *testing.T) *testHarness {
	t.Helper()
	st, err := store.Open(context.Background(), ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { st.Close() })
	fake := clock.NewFake(time.Date(2026, 9, 23, 10, 0, 0, 0, time.UTC))
	s := &Server{Store: st, Clock: fake, Secret: []byte("integration-secret")}
	ts := httptest.NewServer(s.Routes())
	t.Cleanup(ts.Close)
	return &testHarness{t: t, srv: ts, client: ts.Client(), base: ts.URL}
}

func (h *testHarness) do(method, path string, body any) (int, map[string]any) {
	h.t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req, _ := http.NewRequest(method, h.base+path, rdr)
	resp, err := h.client.Do(req)
	if err != nil {
		h.t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func putConfig(h *testHarness, name string, cfg map[string]any) (int, map[string]any) {
	return h.do("PUT", "/api/v1/workloads/"+name+"/config", cfg)
}

func stdConfig() map[string]any {
	return map[string]any{
		"minReplicas": 1, "maxReplicas": 10, "targetPct": 50,
		"tolerancePct": 10, "stableWindowSec": 60,
	}
}

func podsJSON(utils ...any) []map[string]any {
	out := make([]map[string]any, len(utils))
	for i, u := range utils {
		out[i] = map[string]any{"name": string(rune('a' + i)), "ready": true, "utilizationPct": u}
	}
	return out
}

func decisionBody(metricTime string, current int, pods []map[string]any) map[string]any {
	return map[string]any{"metricTime": metricTime, "currentReplicas": current, "pods": pods}
}

func asInt(v any) int {
	switch n := v.(type) {
	case float64:
		return int(n)
	case int:
		return n
	}
	return -1
}

func TestHealthAndConfigLifecycle(t *testing.T) {
	h := newHarness(t)

	if code, body := h.do("GET", "/healthz", nil); code != 200 || body["status"] != "ok" {
		t.Fatalf("health: %d %v", code, body)
	}

	code, body := putConfig(h, "web", stdConfig())
	if code != 200 {
		t.Fatalf("put config: %d %v", code, body)
	}
	v1, _ := body["configVersion"].(string)

	// Deterministic version: same config -> same hash; computed with real SHA-256.
	wantV, _ := crypto.ConfigVersion(1, 10, 50, 10, 60)
	if v1 != wantV {
		t.Fatalf("configVersion=%q want %q", v1, wantV)
	}

	// GET round-trips it.
	code, body = h.do("GET", "/api/v1/workloads/web/config", nil)
	if code != 200 || body["configVersion"] != v1 {
		t.Fatalf("get config: %d %v", code, body)
	}

	// Missing workload and invalid configs.
	if code, _ = h.do("GET", "/api/v1/workloads/nope/config", nil); code != 404 {
		t.Fatalf("want 404, got %d", code)
	}
	bad := stdConfig()
	bad["targetPct"] = 0
	if code, body = putConfig(h, "web", bad); code != 400 || body["error"].(map[string]any)["code"] != "ZERO_TARGET" {
		t.Fatalf("zero target: %d %v", code, body)
	}
	bad = stdConfig()
	bad["maxReplicas"] = 0
	if code, _ = putConfig(h, "bad", bad); code != 400 {
		t.Fatalf("maxReplicas 0: want 400, got %d", code)
	}
}

func TestDecisionScaleUpRawCappedAndReasons(t *testing.T) {
	h := newHarness(t)
	putConfig(h, "web", stdConfig())

	// 4 pods at 90% -> ratio 1.8 -> raw 8, immediately applied.
	body := decisionBody("2026-09-23T10:00:00Z", 4, podsJSON(90, 90, 90, 90))
	code, resp := h.do("POST", "/api/v1/workloads/web/decisions", body)
	if code != 201 {
		t.Fatalf("post: %d %v", code, resp)
	}
	if asInt(resp["rawProposed"]) != 8 || asInt(resp["finalReplicas"]) != 8 || resp["action"] != "scaleup" {
		t.Fatalf("resp=%v", resp)
	}
	if resp["configVersion"] == nil || resp["metricTimeMs"] == nil {
		t.Fatal("decision must bind config version and metric time")
	}
	reasons := resp["reasons"].([]any)
	joined, _ := json.Marshal(reasons)
	if !strings.Contains(string(joined), "SCALE_UP_IMMEDIATE") || !strings.Contains(string(joined), "RAW_ROUNDED_UP") {
		t.Fatalf("reasons=%v", reasons)
	}
	if sig, _ := resp["signature"].(string); len(sig) != 64 {
		t.Fatalf("signature missing/not hex: %v", resp["signature"])
	}
}

func TestMaxReplicasLimitOverHTTP(t *testing.T) {
	h := newHarness(t)
	cfg := stdConfig()
	cfg["maxReplicas"] = 5
	putConfig(h, "cap", cfg)

	pods := make([]map[string]any, 10)
	for i := range pods {
		pods[i] = map[string]any{"name": "p", "ready": true, "utilizationPct": 200}
	}
	code, resp := h.do("POST", "/api/v1/workloads/cap/decisions",
		decisionBody("2026-09-23T10:00:00Z", 10, pods))
	if code != 201 {
		t.Fatalf("post: %d %v", code, resp)
	}
	if asInt(resp["rawProposed"]) != 40 || asInt(resp["finalReplicas"]) != 5 {
		t.Fatalf("raw=%v final=%v, want raw 40 capped to 5", resp["rawProposed"], resp["finalReplicas"])
	}
	joined, _ := json.Marshal(resp["reasons"])
	if !strings.Contains(string(joined), "CAPPED_AT_MAX_REPLICAS") {
		t.Fatalf("reasons=%v", resp["reasons"])
	}
}

func TestMissingMetricsAndUnreadyOverHTTP(t *testing.T) {
	h := newHarness(t)
	putConfig(h, "web", stdConfig())

	// All missing: null utilization, no zero fill.
	pods := []map[string]any{
		{"name": "a", "ready": true, "utilizationPct": nil},
		{"name": "b", "ready": true, "utilizationPct": nil},
	}
	code, resp := h.do("POST", "/api/v1/workloads/web/decisions",
		decisionBody("2026-09-23T10:00:00Z", 4, pods))
	if code != 201 || resp["action"] != "hold" || asInt(resp["finalReplicas"]) != 4 {
		t.Fatalf("all missing: %d %v", code, resp)
	}
	if resp["metricPresent"] != false || asInt(resp["readyMissing"]) != 2 {
		t.Fatalf("metricPresent must be false, readyMissing 2: %v", resp)
	}
	if resp["rawProposed"] != nil {
		t.Fatalf("no raw proposal allowed for missing metrics: %v", resp["rawProposed"])
	}

	// Unready pods carrying a 0 metric must be excluded: 2 ready @80 + 1 unready.
	pods2 := []map[string]any{
		{"name": "a", "ready": true, "utilizationPct": 80},
		{"name": "b", "ready": true, "utilizationPct": 80},
		{"name": "c", "ready": false, "utilizationPct": 0},
	}
	code, resp = h.do("POST", "/api/v1/workloads/web/decisions",
		decisionBody("2026-09-23T10:01:00Z", 3, pods2))
	if code != 201 {
		t.Fatalf("unready: %d %v", code, resp)
	}
	if asInt(resp["unreadyCount"]) != 1 || asInt(resp["avgUtilizationPct"]) != 80 {
		t.Fatalf("unready pod leaked into average: %v", resp)
	}
}

func TestStableWindowSequenceOverHTTP(t *testing.T) {
	h := newHarness(t)
	cfg := stdConfig()
	cfg["stableWindowSec"] = 60
	putConfig(h, "web", cfg)

	post := func(ts string, current int, n int, util float64) map[string]any {
		h.t.Helper()
		ps := make([]map[string]any, n)
		for i := range ps {
			ps[i] = map[string]any{"name": "p", "ready": true, "utilizationPct": util}
		}
		code, resp := h.do("POST", "/api/v1/workloads/web/decisions", decisionBody(ts, current, ps))
		if code != 201 {
			h.t.Fatalf("%s: code %d body %v", ts, code, resp)
		}
		return resp
	}

	// t=10s: high load on 4 pods -> immediate scale up to 8 (raw8@10000).
	r := post("2026-09-23T10:00:10Z", 4, 4, 90)
	if asInt(r["finalReplicas"]) != 8 {
		t.Fatalf("scale up: %v", r)
	}
	// t=40s: load low at 8 pods -> raw 3, but window still contains 8 -> hold.
	r = post("2026-09-23T10:00:40Z", 8, 8, 18.75)
	if asInt(r["rawProposed"]) != 3 || asInt(r["finalReplicas"]) != 8 || r["action"] != "hold" {
		t.Fatalf("window hold: raw=%v final=%v action=%v", r["rawProposed"], r["finalReplicas"], r["action"])
	}
	// t=70s: cutoff is 10s and the window is left-closed, so raw8@10s still
	// counts -> hold.
	r = post("2026-09-23T10:01:10Z", 8, 8, 18.75)
	if asInt(r["finalReplicas"]) != 8 {
		t.Fatalf("window edge should still hold 8: %v", r["finalReplicas"])
	}
	// t=80s: proposal of 8 aged out (cutoff 20s); later raw proposals are 3 -> down.
	r = post("2026-09-23T10:01:20Z", 8, 8, 18.75)
	if asInt(r["finalReplicas"]) != 3 || r["action"] != "scaledown" {
		t.Fatalf("scale down after window: final=%v action=%v", r["finalReplicas"], r["action"])
	}
}

func TestEventTimeRegressionRejectedOverHTTP(t *testing.T) {
	h := newHarness(t)
	putConfig(h, "web", stdConfig())

	body := decisionBody("2026-09-23T10:05:00Z", 4, podsJSON(90, 90, 90, 90))
	code, newer := h.do("POST", "/api/v1/workloads/web/decisions", body)
	if code != 201 {
		t.Fatalf("first post: %d %v", code, newer)
	}

	// An older metric arrives out of order: 409 and the stored decision returned.
	old := decisionBody("2026-09-23T10:00:00Z", 4, podsJSON(10, 10, 10, 10))
	code, resp := h.do("POST", "/api/v1/workloads/web/decisions", old)
	if code != 409 {
		t.Fatalf("want 409, got %d: %v", code, resp)
	}
	if resp["error"].(map[string]any)["code"] != "STALE_METRIC_TIME" {
		t.Fatalf("error=%v", resp["error"])
	}
	ex := resp["existingDecision"].(map[string]any)
	if asInt(ex["finalReplicas"]) != 8 {
		t.Fatalf("existing decision not returned: %v", ex)
	}

	// Latest endpoint still shows the NEWER recommendation (not overwritten).
	code, latest := h.do("GET", "/api/v1/workloads/web/decisions/latest", nil)
	if code != 200 || asInt(latest["finalReplicas"]) != 8 {
		t.Fatalf("latest overwritten by stale event: %d %v", code, latest)
	}

	// Equal timestamp is a duplicate and is also refused.
	dup := decisionBody("2026-09-23T10:05:00Z", 4, podsJSON(10, 10, 10, 10))
	if code, _ = h.do("POST", "/api/v1/workloads/web/decisions", dup); code != 409 {
		t.Fatalf("duplicate timestamp: want 409 got %d", code)
	}
}

func TestConfigVersionBindsDecisionsAndResetsWindow(t *testing.T) {
	h := newHarness(t)
	putConfig(h, "web", stdConfig())

	// First scale-up under v1, then load drops while v1 window holds at 8.
	h.do("POST", "/api/v1/workloads/web/decisions",
		decisionBody("2026-09-23T10:00:00Z", 4, podsJSON(90, 90, 90, 90)))

	cfg2 := stdConfig()
	cfg2["targetPct"] = 70 // changes the config -> new version
	code, c2 := putConfig(h, "web", cfg2)
	if code != 200 {
		t.Fatal(c2)
	}
	v2 := c2["configVersion"].(string)

	// New decision binds to v2 and has an EMPTY window (v1 proposals isolated).
	code, resp := h.do("POST", "/api/v1/workloads/web/decisions",
		decisionBody("2026-09-23T10:00:30Z", 8, podsJSON(20, 20, 20, 20, 20, 20, 20, 20)))
	if code != 201 {
		t.Fatalf("post v2: %d %v", code, resp)
	}
	if resp["configVersion"] != v2 {
		t.Fatalf("decision bound to %v, want %s", resp["configVersion"], v2)
	}
	// Empty fresh window -> conservative hold, not using v1's proposal of 8
	// semantics nor scaling down blindly.
	joined, _ := json.Marshal(resp["reasons"])
	if !strings.Contains(string(joined), "STABLE_WINDOW_NO_PROPOSAL_HOLD") {
		t.Fatalf("new version must start with empty window: %v", resp["reasons"])
	}
}

func TestSignatureVerifyEndpoint(t *testing.T) {
	h := newHarness(t)
	putConfig(h, "web", stdConfig())
	_, d := h.do("POST", "/api/v1/workloads/web/decisions",
		decisionBody("2026-09-23T10:00:00Z", 4, podsJSON(90, 90, 90, 90)))

	// Replaying the stored decision verbatim must verify (real HMAC check).
	code, ok := h.do("POST", "/api/v1/workloads/web/decisions/verify", d)
	if code != 200 || ok["valid"] != true {
		t.Fatalf("verify valid decision: %d %v", code, ok)
	}

	// Tamper finalReplicas -> verification fails.
	tampered := map[string]any{}
	for k, v := range d {
		tampered[k] = v
	}
	tampered["finalReplicas"] = 9
	code, ok = h.do("POST", "/api/v1/workloads/web/decisions/verify", tampered)
	if code != 200 || ok["valid"] != false {
		t.Fatalf("tampered decision must fail: %d %v", code, ok)
	}
}

func TestLatestAndListEndpoints(t *testing.T) {
	h := newHarness(t)
	putConfig(h, "web", stdConfig())
	h.do("POST", "/api/v1/workloads/web/decisions",
		decisionBody("2026-09-23T10:00:00Z", 4, podsJSON(90, 90, 90, 90)))
	h.do("POST", "/api/v1/workloads/web/decisions",
		decisionBody("2026-09-23T10:01:00Z", 8, podsJSON(50, 50, 50, 50, 50, 50, 50, 50)))

	if code, latest := h.do("GET", "/api/v1/workloads/web/decisions/latest", nil); code != 200 {
		t.Fatalf("latest: %d", code)
	} else if int64(asInt(latest["metricTimeMs"])) != int64(time.Date(2026, 9, 23, 10, 1, 0, 0, time.UTC).UnixMilli()) {
		t.Fatalf("latest not newest: %v", latest["metricTimeMs"])
	}
	code, list := h.do("GET", "/api/v1/workloads/web/decisions", nil)
	if code != 200 || len(list["decisions"].([]any)) != 2 {
		t.Fatalf("list: %d %v", code, list)
	}
}
