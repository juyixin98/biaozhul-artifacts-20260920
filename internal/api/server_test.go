package api

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/example/rollout/internal/config"
	"github.com/example/rollout/internal/crypto"
	"github.com/example/rollout/internal/eval"
	"github.com/example/rollout/internal/metrics"
	"github.com/example/rollout/internal/metricstub"
	"github.com/example/rollout/internal/store"
)

type harness struct {
	api    *httptest.Server
	mem    *store.Memory
	keyID  string
	secret []byte
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	stubSrv := httptest.NewServer(metricstub.Handler())
	t.Cleanup(stubSrv.Close)

	mem := store.NewMemory()
	cfg := config.Config{
		StubURL:       stubSrv.URL,
		HMACKeyID:     "test-key",
		HMACSecret:    []byte("test-secret-1234"),
		ObservationMS: 400,
		MinSamples:    80,
	}
	ev := eval.New(metrics.NewClient(stubSrv.URL))
	rel := eval.NewReleaser(mem, ev)
	srv := NewServer(cfg, mem, rel)
	apiSrv := httptest.NewServer(srv.Router)
	t.Cleanup(apiSrv.Close)
	return &harness{api: apiSrv, mem: mem, keyID: "test-key", secret: []byte("test-secret-1234")}
}

// signed performs a mutating request with a fresh HMAC signature+nonce.
func (h *harness) signed(t *testing.T, method, path string, body []byte, idemKey string) (int, map[string]any) {
	t.Helper()
	hdr, err := crypto.Sign(h.keyID, h.secret, method, path, time.Now().Unix(), body)
	if err != nil {
		t.Fatal(err)
	}
	resp := h.request(t, method, path, body, map[string]string{crypto.IdempotencyHeader: idemKey, crypto.HMACHeader: hdr})
	defer resp.Body.Close()
	return resp.StatusCode, decodeMap(t, resp.Body)
}

func (h *harness) request(t *testing.T, method, path string, body []byte, headers map[string]string) *http.Response {
	t.Helper()
	var rdr io.Reader
	if body != nil {
		rdr = bytes.NewReader(body)
	}
	req, err := http.NewRequest(method, h.api.URL+path, rdr)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	for k, v := range headers {
		if v != "" {
			req.Header.Set(k, v)
		}
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	return resp
}

func (h *harness) get(t *testing.T, path string) (int, map[string]any) {
	t.Helper()
	resp := h.request(t, http.MethodGet, path, nil, nil)
	defer resp.Body.Close()
	return resp.StatusCode, decodeMap(t, resp.Body)
}

func (h *harness) getArray(t *testing.T, path string) []map[string]any {
	t.Helper()
	resp := h.request(t, http.MethodGet, path, nil, nil)
	defer resp.Body.Close()
	var out []map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	return out
}

func decodeMap(t *testing.T, r io.Reader) map[string]any {
	t.Helper()
	var out map[string]any
	raw, _ := io.ReadAll(r)
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &out); err != nil {
			t.Fatalf("decode %q: %v", string(raw), err)
		}
	}
	return out
}

func createRelease(t *testing.T, h *harness, scenario string) map[string]any {
	t.Helper()
	body := []byte(fmt.Sprintf(`{"name":"svc","version":"v1","scenario":%q}`, scenario))
	status, out := h.signed(t, http.MethodPost, "/api/releases", body, "")
	if status != http.StatusCreated {
		t.Fatalf("create release status=%d body=%v", status, out)
	}
	return out
}

func relID(m map[string]any) string { return m["id"].(string) }

func TestCreateRequiresSignature(t *testing.T) {
	h := newHarness(t)
	resp := h.request(t, http.MethodPost, "/api/releases", []byte(`{"name":"x","version":"y"}`), nil)
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("unsigned POST status=%d want 401", resp.StatusCode)
	}
}

func TestBadSignatureRejected(t *testing.T) {
	h := newHarness(t)
	resp := h.request(t, http.MethodPost, "/api/releases", []byte(`{}`),
		map[string]string{crypto.HMACHeader: `kid="test-key",ts=999,nonce="abcd",sig="deadbeef"`})
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("bad signature status=%d want 401", resp.StatusCode)
	}
}

func TestReplayRejected(t *testing.T) {
	h := newHarness(t)
	body := []byte(`{"name":"svc","version":"v1"}`)
	ts := time.Now().Unix()
	nonce := "fixednonce0000000000000000000000aa"
	hdr := crypto.SignWithNonce(h.keyID, h.secret, "POST", "/api/releases", ts, nonce, body)

	send := func() int {
		resp := h.request(t, "POST", "/api/releases", body,
			map[string]string{crypto.HMACHeader: hdr})
		code := resp.StatusCode
		resp.Body.Close()
		return code
	}
	if first := send(); first != http.StatusCreated {
		t.Fatalf("first request status=%d want 201", first)
	}
	if second := send(); second != http.StatusUnauthorized {
		t.Fatalf("replay status=%d want 401", second)
	}
}

func TestIdempotencyKeyReturnsCachedResponse(t *testing.T) {
	h := newHarness(t)
	body := []byte(`{"name":"svc","version":"v1"}`)
	s1, o1 := h.signed(t, "POST", "/api/releases", body, "idem-123")
	s2, o2 := h.signed(t, "POST", "/api/releases", body, "idem-123")
	if s1 != http.StatusCreated || s2 != http.StatusCreated {
		t.Fatalf("statuses %d %d", s1, s2)
	}
	if relID(o1) != relID(o2) {
		t.Fatalf("idempotent retries must return the same release id: %v vs %v", o1["id"], o2["id"])
	}
}

func TestHealthyAdvanceFlowOverHTTP(t *testing.T) {
	h := newHarness(t)
	id := relID(createRelease(t, h, "healthy"))

	// Early evaluate -> unknown (window not complete).
	st, ev := h.get(t, "/api/releases/"+id+"/evaluate")
	obs := ev["observation"].(map[string]any)
	if st != 200 || obs["Verdict"].(map[string]any)["health"] != "unknown" {
		t.Fatalf("early evaluate: %d %v", st, ev)
	}

	time.Sleep(520 * time.Millisecond)

	// Evaluate -> healthy with interval evidence.
	_, ev = h.get(t, "/api/releases/"+id+"/evaluate")
	verdict := ev["observation"].(map[string]any)["Verdict"].(map[string]any)
	if verdict["health"] != "healthy" {
		t.Fatalf("want healthy, got %v", verdict)
	}
	mm := verdict["metrics"].(map[string]any)
	if mm["error_rate_upper"] == nil || mm["latency_upper_ms"] == nil {
		t.Fatalf("missing interval evidence: %v", mm)
	}
	if ev["frozen_threshold_version"].(float64) != 1 {
		t.Fatalf("threshold must be frozen at 1: %v", ev["frozen_threshold_version"])
	}

	// Advance with the correct generation 0.
	body := []byte(`{"expected_generation":0}`)
	st, out := h.signed(t, "POST", "/api/releases/"+id+"/commands/advance", body, "")
	if st != http.StatusOK || out["applied"] != true {
		t.Fatalf("advance failed: %d %v", st, out)
	}
	rl := out["release"].(map[string]any)
	if rl["stage"] != "20%" || int(rl["generation"].(float64)) != 1 {
		t.Fatalf("unexpected post-advance state: %v", rl)
	}

	// Retry with the stale generation 0 -> 409, state unchanged.
	st, out = h.signed(t, "POST", "/api/releases/"+id+"/commands/advance", body, "")
	if st != http.StatusConflict {
		t.Fatalf("stale generation status=%d want 409 body=%v", st, out)
	}
}

func TestDegradedAdvanceRejectedOverHTTP(t *testing.T) {
	h := newHarness(t)
	id := relID(createRelease(t, h, "degraded"))
	time.Sleep(520 * time.Millisecond)
	st, out := h.signed(t, "POST", "/api/releases/"+id+"/commands/advance",
		[]byte(`{"expected_generation":0}`), "")
	if st != http.StatusConflict || out["applied"] != false {
		t.Fatalf("degraded advance status=%d body=%v", st, out)
	}
	rej := out["rejection"].(map[string]any)
	if rej["verdict"].(map[string]any)["health"] != "degraded" {
		t.Fatalf("rejection verdict must be degraded: %v", rej)
	}
}

func TestPauseResumeOverHTTP(t *testing.T) {
	h := newHarness(t)
	id := relID(createRelease(t, h, "healthy"))

	st, out := h.signed(t, "POST", "/api/releases/"+id+"/commands/pause",
		[]byte(`{"expected_generation":0}`), "")
	if st != 200 || out["applied"] != true {
		t.Fatalf("pause failed: %d %v", st, out)
	}
	st, out = h.signed(t, "POST", "/api/releases/"+id+"/commands/resume",
		[]byte(`{"expected_generation":1}`), "")
	if st != 200 || out["applied"] != true {
		t.Fatalf("resume failed: %d %v", st, out)
	}
	// Re-using the now-stale generation 1 conflicts.
	st, _ = h.signed(t, "POST", "/api/releases/"+id+"/commands/pause",
		[]byte(`{"expected_generation":1}`), "")
	if st != http.StatusConflict {
		t.Fatalf("stale pause status=%d want 409", st)
	}
}

func TestUnknownCommandRejected(t *testing.T) {
	h := newHarness(t)
	id := relID(createRelease(t, h, "healthy"))
	st, _ := h.signed(t, "POST", "/api/releases/"+id+"/commands/explode",
		[]byte(`{"expected_generation":0}`), "")
	if st != http.StatusBadRequest {
		t.Fatalf("unknown command status=%d want 400", st)
	}
}

func TestEventsAndObservationsRecorded(t *testing.T) {
	h := newHarness(t)
	id := relID(createRelease(t, h, "degraded"))
	time.Sleep(520 * time.Millisecond)
	st, out := h.signed(t, "POST", "/api/releases/"+id+"/commands/rollback",
		[]byte(`{"expected_generation":0}`), "")
	if st != 200 || out["applied"] != true {
		t.Fatalf("rollback failed: %d %v", st, out)
	}

	events := h.getArray(t, "/api/releases/"+id+"/events")
	var sawRollback bool
	for _, e := range events {
		if e["type"] == "rolled_back" {
			sawRollback = true
		}
	}
	if !sawRollback {
		t.Fatalf("expected a rolled_back event, got %v", events)
	}

	observations := h.getArray(t, "/api/releases/"+id+"/observations")
	if len(observations) == 0 {
		t.Fatal("expected at least one observation carrying evidence")
	}
	first := observations[0]
	verdict := first["Verdict"].(map[string]any)
	if verdict["metrics"] == nil {
		t.Fatalf("observation must embed metric evidence: %v", first)
	}
}

func TestThresholdVersioningEndpoints(t *testing.T) {
	h := newHarness(t)
	// Latest starts at v1 default.
	st, latest := h.get(t, "/api/thresholds/latest")
	if st != 200 || int(latest["version"].(float64)) != 1 {
		t.Fatalf("latest threshold: %d %v", st, latest)
	}
	// Tighten.
	body := []byte(`{"error_rate_upper":0.01,"latency_mean_ms":60,"description":"stricter"}`)
	st, created := h.signed(t, "POST", "/api/thresholds", body, "")
	if st != http.StatusCreated || int(created["version"].(float64)) != 2 {
		t.Fatalf("create threshold: %d %v", st, created)
	}
}
