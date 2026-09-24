// Package integration contains black-box tests that drive the full server
// over HTTP with a real SQLite database and a controllable clock.
package integration

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"
	"time"

	"scaler-stable-window/internal/api"
	"scaler-stable-window/internal/clock"
	"scaler-stable-window/internal/hpa"
	"scaler-stable-window/internal/store"
)

type harness struct {
	t   *testing.T
	srv *httptest.Server
	now time.Time
}

const start = "2026-09-23T12:00:00Z"

func newHarness(t *testing.T, maxReplicas int) *harness {
	t.Helper()
	dbFile := filepath.Join(t.TempDir(), "test.db")
	st, err := store.New(dbFile)
	if err != nil {
		t.Fatalf("store: %v", err)
	}
	t.Cleanup(func() { st.Close() })

	t0, _ := time.Parse(time.RFC3339, start)
	clk := clock.NewManual(t0)
	svc := hpa.NewService(st, []byte("integration-key"))
	srv := httptest.NewServer(api.NewServer(svc, st, clk).Handler())
	t.Cleanup(srv.Close)

	h := &harness{t: t, srv: srv, now: t0}

	body := map[string]any{
		"targetUtilization":                   70,
		"tolerancePct":                        10,
		"minReplicas":                         1,
		"maxReplicas":                         maxReplicas,
		"scaleDownStabilizationWindowSeconds": 300,
		"scaleUpStabilizationWindowSeconds":   0,
		"metricFreshnessSeconds":              120,
	}
	resp, err := h.put("/scalers/app/config", body)
	if err != nil {
		t.Fatalf("put config: %v", err)
	}
	if resp.StatusCode != 201 {
		t.Fatalf("put config status=%d", resp.StatusCode)
	}
	return h
}

func (h *harness) put(path string, v any) (*http.Response, error) {
	b, _ := json.Marshal(v)
	req, _ := http.NewRequest(http.MethodPut, h.srv.URL+path, bytes.NewReader(b))
	req.Header.Set("Content-Type", "application/json")
	return http.DefaultClient.Do(req)
}

func (h *harness) post(path string, v any) (*http.Response, error) {
	b, _ := json.Marshal(v)
	req, _ := http.NewRequest(http.MethodPost, h.srv.URL+path, bytes.NewReader(b))
	req.Header.Set("Content-Type", "application/json")
	return http.DefaultClient.Do(req)
}

func (h *harness) get(path string) (*http.Response, error) {
	return http.Get(h.srv.URL + path)
}

func (h *harness) advance(seconds int) {
	h.now = h.now.Add(time.Duration(seconds) * time.Second)
	resp, err := h.post("/clock/advance", map[string]int{"seconds": seconds})
	if err != nil {
		h.t.Fatalf("advance clock: %v", err)
	}
	resp.Body.Close()
}

type inst struct {
	name string
	rdy  bool
	util *float64
}

type decision struct {
	ConfigVersion          int      `json:"configVersion"`
	MetricTimestamp        string   `json:"metricTimestamp"`
	CurrentReplicas        int      `json:"currentReplicas"`
	ReadyReporting         int      `json:"readyReporting"`
	ReadyMissing           int      `json:"readyMissing"`
	RawDesired             int      `json:"rawDesired"`
	RawAction              string   `json:"rawAction"`
	WindowedDesired        int      `json:"windowedDesired"`
	FinalDesired           int      `json:"finalDesired"`
	FinalAction            string   `json:"finalAction"`
	FinalReasons           []string `json:"reasons"`
	RecommendationRecorded bool     `json:"recommendationRecorded"`
	Signature              string   `json:"signature"`
}

func (h *harness) decide(replicas int, instances []inst) decision {
	h.t.Helper()
	list := make([]map[string]any, 0, len(instances))
	for _, in := range instances {
		m := map[string]any{"name": in.name, "ready": in.rdy}
		if in.util != nil {
			m["utilizationPct"] = *in.util
		}
		list = append(list, m)
	}
	req := map[string]any{
		"configVersion":   1,
		"currentReplicas": replicas,
		"metricTimestamp": h.now.Format(time.RFC3339),
		"instances":       list,
	}
	resp, err := h.post("/scalers/app/decide", req)
	if err != nil {
		h.t.Fatalf("decide: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		var e map[string]any
		_ = json.NewDecoder(resp.Body).Decode(&e)
		h.t.Fatalf("decide status=%d body=%v", resp.StatusCode, e)
	}
	var d decision
	if err := json.NewDecoder(resp.Body).Decode(&d); err != nil {
		h.t.Fatalf("decode: %v", err)
	}
	return d
}

func readyPods(n int, util float64) []inst {
	out := make([]inst, n)
	for i := range out {
		u := util
		out[i] = inst{name: fmt.Sprintf("p%d", i), rdy: true, util: &u}
	}
	return out
}

func missingPods(n int) []inst {
	out := make([]inst, n)
	for i := range out {
		out[i] = inst{name: fmt.Sprintf("p%d", i), rdy: true, util: nil}
	}
	return out
}

// Scenario 1: step load. Quiet fleet at 70% jumps to 100% (fast scale-up),
// then back to 10% (scale-down deferred by the 300s stable window and then
// executed).
func TestScenario_StepLoad(t *testing.T) {
	h := newHarness(t, 10)

	// steady state: 4 at 70% -> hold
	d := h.decide(4, readyPods(4, 70))
	if d.FinalAction != "Hold" || d.FinalDesired != 4 {
		t.Fatalf("steady: action=%s final=%d", d.FinalAction, d.FinalDesired)
	}

	// step up to 100% at +30s -> ceil(4*100/70)=6 immediately (up window 0)
	h.advance(30)
	d = h.decide(4, readyPods(4, 100))
	if d.RawDesired != 6 || d.FinalDesired != 6 || d.FinalAction != "ScaleUp" {
		t.Fatalf("step up: raw=%d final=%d action=%s", d.RawDesired, d.FinalDesired, d.FinalAction)
	}

	// load drops to 10% at +60s; orchestrator has applied 6. Raw wants 1 but
	// the window max keeps 6.
	h.advance(30)
	d = h.decide(6, readyPods(6, 10))
	if d.RawDesired != 1 || d.FinalDesired != 6 || d.FinalAction != "Hold" {
		t.Fatalf("step down blocked: raw=%d final=%d action=%s", d.RawDesired, d.FinalDesired, d.FinalAction)
	}

	// low load ticks every 60s; window keeps holding 6 until the spike record
	// ages out (>300s after the spike).
	for i := 0; i < 4; i++ {
		h.advance(60)
		d = h.decide(6, readyPods(6, 10))
		if d.FinalDesired != 6 {
			t.Fatalf("tick %d: window released early to %d", i, d.FinalDesired)
		}
	}
	// spike was at now-base; after another 61s the record is >300s old
	h.advance(61)
	d = h.decide(6, readyPods(6, 10))
	if d.RawDesired != 1 || d.FinalDesired != 1 || d.FinalAction != "ScaleDown" {
		t.Fatalf("stable window should release: raw=%d final=%d action=%s", d.RawDesired, d.FinalDesired, d.FinalAction)
	}
}

// Scenario 2: periodic fluctuation inside/around the band must not cause
// flapping — tolerance holds, and out-of-band dips cannot shrink the fleet
// while the window contains a high recommendation.
func TestScenario_PeriodicFluctuation(t *testing.T) {
	h := newHarness(t, 10)

	// spike to 6 first
	d := h.decide(4, readyPods(4, 100))
	if d.FinalDesired != 6 {
		t.Fatalf("initial spike final=%d", d.FinalDesired)
	}

	// oscillate every 30s: 65% (in band), 78% (slightly up), 65%, 20% (down
	// raw but window blocks), 65%...
	wave := []float64{65, 78, 65, 20, 65, 78, 65, 20}
	for i, util := range wave {
		h.advance(30)
		d = h.decide(6, readyPods(6, util))
		// nothing may shrink below 6 while the spike is in the window
		if d.FinalDesired < 6 {
			t.Fatalf("wave[%d]=%.0f shrank to %d (flapping)", i, util, d.FinalDesired)
		}
		// 65% is inside the tolerance band: the *formula* holds even though
		// the stable-window max can still keep FinalAction at 6.
		if util == 65 && d.RawAction != "Hold" {
			t.Fatalf("wave[%d]=%.0f formula should hold in tolerance band, raw=%s", i, util, d.RawAction)
		}
		if util == 20 && d.RawAction != "ScaleDown" {
			t.Fatalf("wave[%d]=%.0f raw should be ScaleDown, got %s", i, util, d.RawAction)
		}
	}
}

// Scenario 3: long metric outage. Missing samples are not zero-filled: the
// fleet is held, and after metrics return the stale-but-not-missing recovery
// still cannot shrink until the old high recommendation leaves the window.
func TestScenario_LongMetricOutage(t *testing.T) {
	h := newHarness(t, 10)

	// establish a high recommendation
	d := h.decide(4, readyPods(4, 100)) // final 6
	if d.FinalDesired != 6 {
		t.Fatalf("setup final=%d", d.FinalDesired)
	}

	// metrics disappear for 10 minutes (30s ticks); every decision must hold
	// at 6 and record no recommendation.
	for i := 1; i <= 20; i++ {
		h.advance(30)
		d = h.decide(6, missingPods(6))
		if d.FinalDesired != 6 || d.FinalAction != "Hold" {
			t.Fatalf("outage tick %d: final=%d action=%s", i, d.FinalDesired, d.FinalAction)
		}
		if d.RecommendationRecorded {
			t.Fatalf("outage tick %d must not record a recommendation", i)
		}
	}

	// metrics return at low load. The last recorded recommendation is now
	// 600s old and outside the 300s window, so the fresh raw 1 applies.
	h.advance(30)
	d = h.decide(6, readyPods(6, 10))
	if d.FinalDesired != 1 || d.FinalAction != "ScaleDown" {
		t.Fatalf("recovery: final=%d action=%s, want 1/ScaleDown", d.FinalDesired, d.FinalAction)
	}
}

// Scenario 4: maxReplicas ceiling under sustained overload.
func TestScenario_MaxReplicasCap(t *testing.T) {
	h := newHarness(t, 5) // tight ceiling

	d := h.decide(5, readyPods(5, 100)) // ceil(500/70)=8 -> cap 5
	if d.RawDesired != 5 || d.FinalDesired != 5 || d.FinalAction != "Hold" {
		t.Fatalf("ceiling: raw=%d final=%d action=%s", d.RawDesired, d.FinalDesired, d.FinalAction)
	}
	found := false
	for _, r := range d.FinalReasons {
		if r == "capped_at_max_replicas" {
			found = true
		}
	}
	if !found {
		t.Fatalf("reasons %v missing capped_at_max_replicas", d.FinalReasons)
	}

	// smaller fleet under same overload can still grow up to the ceiling
	h.advance(30)
	d = h.decide(3, readyPods(3, 100)) // ceil(300/70)=5 -> 5
	if d.FinalDesired != 5 || d.FinalAction != "ScaleUp" {
		t.Fatalf("grow to ceiling: final=%d action=%s", d.FinalDesired, d.FinalAction)
	}
}

// Event-time regression must be rejected over HTTP (409), and the newer
// decision must remain authoritative.
func TestScenario_MetricTimeRegressionRejected(t *testing.T) {
	h := newHarness(t, 10)
	d := h.decide(4, readyPods(4, 100))
	if d.FinalDesired != 6 {
		t.Fatalf("setup final=%d", d.FinalDesired)
	}
	h.advance(60)
	// 90s older than the newest accepted event (which decided ScaleDown->1);
	// it must be refused regardless of its content.
	stale := h.now.Add(-90 * time.Second)
	req := map[string]any{
		"configVersion":   1,
		"currentReplicas": 6,
		"metricTimestamp": stale.Format(time.RFC3339),
		"instances":       toMaps(readyPods(6, 100)),
	}
	resp, err := h.post("/scalers/app/decide", req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 409 {
		t.Fatalf("regression status=%d, want 409", resp.StatusCode)
	}
	var e map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&e)
	if e["reason"] != "metric_timestamp_regressed" {
		t.Fatalf("error reason=%v", e["reason"])
	}

	// a current-time tick still works; the stable window continues to hold
	// the earlier high recommendation (window max 6, not the late event).
	d = h.decide(6, readyPods(6, 10))
	if d.FinalDesired != 6 {
		t.Fatalf("newer event decided normally but window should still hold 6, got %d", d.FinalDesired)
	}
}

// Zero target utilization is refused at config PUT time (no division by zero
// ever happens).
func TestScenario_ZeroTargetRejected(t *testing.T) {
	h := newHarness(t, 10)
	resp, err := h.put("/scalers/bad/config", map[string]any{
		"targetUtilization": 0, "minReplicas": 1, "maxReplicas": 3,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 422 {
		t.Fatalf("zero target status=%d, want 422", resp.StatusCode)
	}
}

// Config version binding: decision requests must carry the active version;
// every decision is bound to version + fingerprint + metric timestamp.
func TestScenario_ConfigVersionBindingAndReset(t *testing.T) {
	h := newHarness(t, 10)
	d := h.decide(4, readyPods(4, 100)) // v1 high recommendation
	if d.ConfigVersion != 1 || d.Signature == "" || d.MetricTimestamp == "" {
		t.Fatalf("decision binding incomplete: %+v", d)
	}

	// stale version is rejected
	h.advance(30)
	req := map[string]any{
		"configVersion":   99,
		"currentReplicas": 6,
		"metricTimestamp": h.now.Format(time.RFC3339),
		"instances":       toMaps(readyPods(6, 10)),
	}
	resp, err := h.post("/scalers/app/decide", req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != 409 {
		t.Fatalf("stale config version status=%d, want 409", resp.StatusCode)
	}

	// publish v2 (tighter target): stable window restarts, old max cannot
	// block the new policy's decision
	resp2, err := h.put("/scalers/app/config", map[string]any{
		"targetUtilization":                   50,
		"tolerancePct":                        10,
		"minReplicas":                         1,
		"maxReplicas":                         10,
		"scaleDownStabilizationWindowSeconds": 300,
		"scaleUpStabilizationWindowSeconds":   0,
		"metricFreshnessSeconds":              120,
	})
	if err != nil {
		t.Fatal(err)
	}
	resp2.Body.Close()
	if resp2.StatusCode != 201 {
		t.Fatalf("config v2 status=%d", resp2.StatusCode)
	}
	req["configVersion"] = 2
	resp3, err := h.post("/scalers/app/decide", req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp3.Body.Close()
	if resp3.StatusCode != 200 {
		t.Fatalf("v2 decide status=%d", resp3.StatusCode)
	}
	var nd decision
	_ = json.NewDecoder(resp3.Body).Decode(&nd)
	if nd.ConfigVersion != 2 {
		t.Fatalf("decided with version=%d, want 2 (fresh window)", nd.ConfigVersion)
	}
}

// Unready instances are handled separately from missing metrics.
func TestScenario_UnreadyInstances(t *testing.T) {
	h := newHarness(t, 10)
	u := 100.0
	d := h.decide(4, []inst{
		{name: "p0", rdy: true, util: &u},
		{name: "p1", rdy: true, util: &u},
		{name: "p2", rdy: true, util: &u},
		{name: "p3", rdy: false}, // starting pod excluded; counted at 100% on scale-up
	})
	if d.RawDesired != 6 || d.FinalDesired != 6 {
		t.Fatalf("unready scale-up raw=%d final=%d", d.RawDesired, d.FinalDesired)
	}
}

// Every decision is persisted and retrievable from the audit history.
func TestScenario_DecisionHistoryPersisted(t *testing.T) {
	h := newHarness(t, 10)
	h.decide(4, readyPods(4, 100))
	h.advance(30)
	h.decide(6, readyPods(6, 70))

	resp, err := h.get("/scalers/app/decisions?limit=10")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("history status=%d", resp.StatusCode)
	}
	var got struct {
		Decisions []struct {
			Body json.RawMessage `json:"body"`
		} `json:"decisions"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&got); err != nil {
		t.Fatal(err)
	}
	if len(got.Decisions) != 2 {
		t.Fatalf("persisted decisions=%d, want 2", len(got.Decisions))
	}
	if len(got.Decisions[0].Body) == 0 {
		t.Fatal("newest decision body is empty")
	}
}

func toMaps(in []inst) []map[string]any {
	out := make([]map[string]any, len(in))
	for i, in := range in {
		m := map[string]any{"name": in.name, "ready": in.rdy}
		if in.util != nil {
			m["utilizationPct"] = *in.util
		}
		out[i] = m
	}
	return out
}
