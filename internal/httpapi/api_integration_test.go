package httpapi_test

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"sync"
	"testing"
	"time"

	"twap-service/internal/auth"
	"twap-service/internal/domain"
	"twap-service/internal/httpapi"
	"twap-service/internal/service"
	"twap-service/internal/store"
)

type fixedClock struct {
	mu sync.RWMutex
	t  time.Time
}

func (c *fixedClock) now() time.Time {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.t
}
func (c *fixedClock) set(t time.Time) { c.mu.Lock(); c.t = t; c.mu.Unlock() }

func testDSN() string {
	if v := os.Getenv("TWAP_TEST_DSN"); v != "" {
		return v
	}
	return "postgres://twap:twappw@127.0.0.1:5432/twap?sslmode=disable&statement_cache_mode=describe"
}

type harness struct {
	srv    *httptest.Server
	st     *store.Store
	svc    *service.Service
	clock  *fixedClock
	secret string
}

func newHarness(t *testing.T, mode service.ConflictMode) *harness {
	t.Helper()
	if os.Getenv("TWAP_TEST_SKIP_DB") == "1" {
		t.Skip("TWAP_TEST_SKIP_DB=1")
	}
	ctx := context.Background()
	st, err := store.New(ctx, testDSN())
	if err != nil {
		t.Skipf("postgresql unavailable: %v", err)
	}
	t.Cleanup(func() { st.Close() })
	for _, tbl := range []string{"window_versions", "used_nonces", "samples", "sources"} {
		if _, err := st.Pool().Exec(ctx, "TRUNCATE "+tbl+" CASCADE"); err != nil {
			t.Fatal(err)
		}
	}
	clock := &fixedClock{t: time.Date(2026, 3, 1, 12, 0, 0, 0, time.UTC)}
	cfg := domain.Config{WindowSec: 60, LateToleranceSec: 300,
		FutureGraceSec: 2, StaleHorizonSec: 120}
	svc := service.New(st, cfg, mode, clock.now)
	secret := "q1Y9Zk3cQ8vN5sW7xP2rT6yB0mH4jD8gF1lK3nV6eQ=" // valid base64 32B
	if err := svc.RegisterSource(ctx, "venueA", 10, secret); err != nil {
		t.Fatal(err)
	}
	if err := svc.RegisterSource(ctx, "venueB", 0, secret); err != nil {
		t.Fatal(err)
	}
	api := httpapi.New(svc, st, "test-admin", 5*time.Minute)
	srv := httptest.NewServer(api.Router())
	t.Cleanup(srv.Close)
	return &harness{srv: srv, st: st, svc: svc, clock: clock, secret: secret}
}

// signPost performs a real HMAC-signed POST.
func (h *harness) signPost(t *testing.T, path, source string, body []byte,
	ts time.Time, nonce string) (*http.Response, map[string]any) {
	t.Helper()
	sig, err := auth.Sign(h.secret, fmt.Sprintf("%d", ts.UnixMicro()), nonce, body)
	if err != nil {
		t.Fatal(err)
	}
	req, _ := http.NewRequest(http.MethodPost, h.srv.URL+path, bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Source", source)
	req.Header.Set("X-Timestamp-Usec", fmt.Sprintf("%d", ts.UnixMicro()))
	req.Header.Set("X-Nonce", nonce)
	req.Header.Set("X-Signature", sig)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var parsed map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&parsed)
	return resp, parsed
}

func (h *harness) get(t *testing.T, path string) (int, map[string]any) {
	t.Helper()
	resp, err := http.Get(h.srv.URL + path)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var parsed map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&parsed)
	return resp.StatusCode, parsed
}

func sampleBody(symbol string, ts time.Time, price int64) []byte {
	b, _ := json.Marshal(map[string]any{
		"symbol": symbol, "ts_us": ts.UnixMicro(), "price": price})
	return b
}

func TestHTTPIngestHappyPathAndRead(t *testing.T) {
	h := newHarness(t, service.ConflictPriority)
	now := h.clock.now()
	body := sampleBody("BTC", now, 100)
	resp, out := h.signPost(t, "/v1/samples", "venueA", body, now, "nonce-happy")
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("status=%d body=%v", resp.StatusCode, out)
	}
	start := now.Unix() - now.Unix()%60
	status, win := h.get(t, fmt.Sprintf("/v1/symbols/BTC/windows/%d", start))
	if status != http.StatusOK {
		t.Fatalf("read status=%d body=%v", status, win)
	}
	if win["twap"] != "100.000000" {
		t.Fatalf("twap=%v coverage=%v stale=%v", win["twap"], win["coverage"], win["stale"])
	}
	if win["stale"] != true {
		t.Fatal("in-flight window must read stale=true")
	}
	if win["coverage"].(string) == "0.000000" {
		t.Fatal("coverage must be > 0")
	}
}

func TestHTTPBadSignatureRejected(t *testing.T) {
	h := newHarness(t, service.ConflictPriority)
	now := h.clock.now()
	body := sampleBody("BTC", now, 100)
	// Correct signing string but a wrong MAC value.
	req, _ := http.NewRequest(http.MethodPost, h.srv.URL+"/v1/samples", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Source", "venueA")
	req.Header.Set("X-Timestamp-Usec", fmt.Sprintf("%d", now.UnixMicro()))
	req.Header.Set("X-Nonce", "nonce-badsig")
	req.Header.Set("X-Signature", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
	resp, _ := http.DefaultClient.Do(req)
	resp.Body.Close()
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("bad signature status=%d want 401", resp.StatusCode)
	}

	// Unknown source is also 401.
	sig, _ := auth.Sign(h.secret, fmt.Sprintf("%d", now.UnixMicro()), "n-x", body)
	req2, _ := http.NewRequest(http.MethodPost, h.srv.URL+"/v1/samples", bytes.NewReader(body))
	req2.Header.Set("X-Source", "ghost")
	req2.Header.Set("X-Timestamp-Usec", fmt.Sprintf("%d", now.UnixMicro()))
	req2.Header.Set("X-Nonce", "n-x")
	req2.Header.Set("X-Signature", sig)
	resp2, _ := http.DefaultClient.Do(req2)
	resp2.Body.Close()
	if resp2.StatusCode != http.StatusUnauthorized {
		t.Fatalf("unknown source status=%d want 401", resp2.StatusCode)
	}
}

func TestHTTPReplayNonceRejected(t *testing.T) {
	h := newHarness(t, service.ConflictPriority)
	now := h.clock.now()
	body := sampleBody("ETH", now, 100)
	if r, _ := h.signPost(t, "/v1/samples", "venueA", body, now, "nonce-replay"); r.StatusCode != 201 {
		t.Fatal("first request should succeed")
	}
	// Exact replay of method+path+headers+body must be refused.
	resp, out := h.signPost(t, "/v1/samples", "venueA", body, now, "nonce-replay")
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("replay status=%d want 409, body=%v", resp.StatusCode, out)
	}
}

func TestHTTPTimestampSkewRejected(t *testing.T) {
	h := newHarness(t, service.ConflictPriority)
	now := h.clock.now()
	body := sampleBody("SOL", now.Add(-10*time.Minute), 100)
	// Signature timestamp 10 minutes in the past -> skew rejection.
	resp, out := h.signPost(t, "/v1/samples", "venueA", body, now.Add(-10*time.Minute), "nonce-skew")
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("skew status=%d want 401 body=%v", resp.StatusCode, out)
	}
}

func TestHTTPLateSampleRejected422(t *testing.T) {
	h := newHarness(t, service.ConflictPriority)
	now := h.clock.now()
	// Valid request time, but SAMPLE ts is 6 minutes before the clock.
	ts := now.Add(-6 * time.Minute)
	body := sampleBody("XRP", ts, 100)
	resp, out := h.signPost(t, "/v1/samples", "venueA", body, now, "nonce-late")
	if resp.StatusCode != http.StatusUnprocessableEntity {
		t.Fatalf("late sample status=%d want 422 body=%v", resp.StatusCode, out)
	}
}

func TestHTTPLateSampleAcceptedWithin5MinAndVersions(t *testing.T) {
	h := newHarness(t, service.ConflictPriority)
	now := h.clock.now()
	base := time.Unix(now.Unix()-now.Unix()%60, 0).UTC()
	// Seed base prices.
	b0 := sampleBody("ADA", base, 100)
	if r, _ := h.signPost(t, "/v1/samples", "venueA", b0, base.Add(time.Second), "n0"); r.StatusCode != 201 {
		t.Fatal("seed 0 failed")
	}
	b1 := sampleBody("ADA", base.Add(60*time.Second), 100)
	h.clock.set(base.Add(61 * time.Second))
	if r, _ := h.signPost(t, "/v1/samples", "venueA", b1, base.Add(61*time.Second), "n1"); r.StatusCode != 201 {
		t.Fatal("seed 1 failed")
	}
	// Read window to persist v1.
	status, before := h.get(t, fmt.Sprintf("/v1/symbols/ADA/windows/%d", base.Unix()))
	if status != 200 {
		t.Fatalf("read failed: %d", status)
	}
	// Late correction 4 minutes after the fact to t=base+10.
	h.clock.set(base.Add(250 * time.Second))
	corr := sampleBody("ADA", base.Add(10*time.Second), 200)
	resp, out := h.signPost(t, "/v1/samples", "venueA", corr,
		base.Add(250*time.Second), "n-late")
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusCreated {
		t.Fatalf("late correction status=%d body=%v", resp.StatusCode, out)
	}
	_, after := h.get(t, fmt.Sprintf("/v1/symbols/ADA/windows/%d", base.Unix()))
	if after["content_hash"] == before["content_hash"] {
		t.Fatal("late correction must change content hash / create new version")
	}
	if after["twap"] != "183.333333" {
		t.Fatalf("twap after late correction=%v want 183.333333", after["twap"])
	}
	if af64(after["version"]) <= af64(before["version"]) {
		t.Fatalf("version did not advance: %v -> %v", before["version"], after["version"])
	}
}

func TestHTTPSourceConflictRejectMode409(t *testing.T) {
	h := newHarness(t, service.ConflictReject)
	now := h.clock.now()
	b0 := sampleBody("DOGE", now, 100)
	if r, _ := h.signPost(t, "/v1/samples", "venueA", b0, now, "c0"); r.StatusCode != 201 {
		t.Fatal("seed failed")
	}
	b1 := sampleBody("DOGE", now, 101)
	resp, out := h.signPost(t, "/v1/samples", "venueB", b1, now, "c1")
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("conflict status=%d want 409 body=%v", resp.StatusCode, out)
	}
	if out["error"] != "source_conflict" {
		t.Fatalf("error=%v", out["error"])
	}
}

func TestHTTPSourceConflictPriorityModeRecorded(t *testing.T) {
	h := newHarness(t, service.ConflictPriority)
	now := h.clock.now()
	base := time.Unix(now.Unix()-now.Unix()%60, 0).UTC()
	b0 := sampleBody("LTC", base.Add(5*time.Second), 100)
	h.clock.set(base.Add(6 * time.Second))
	if r, _ := h.signPost(t, "/v1/samples", "venueB", b0,
		base.Add(6*time.Second), "p0"); r.StatusCode != 201 {
		t.Fatal("seed failed")
	}
	b1 := sampleBody("LTC", base.Add(5*time.Second), 150)
	if r, out := h.signPost(t, "/v1/samples", "venueA", b1,
		base.Add(7*time.Second), "p1"); r.StatusCode != 201 {
		t.Fatalf("priority-mode upsert: %d %v", r.StatusCode, out)
	}
	h.clock.set(base.Add(60 * time.Second))
	_, win := h.get(t, fmt.Sprintf("/v1/symbols/LTC/windows/%d", base.Unix()))
	if win["twap"] != "150.000000" {
		t.Fatalf("winner twap=%v want 150", win["twap"])
	}
	cfs, _ := win["conflicts"].([]any)
	if len(cfs) == 0 {
		t.Fatal("conflict list must be surfaced in the read response")
	}
}

func TestHTTPAdminAuthAndRebuild(t *testing.T) {
	h := newHarness(t, service.ConflictPriority)
	// Rebuild without credentials -> 401.
	req, _ := http.NewRequest(http.MethodPost, h.srv.URL+"/v1/admin/rebuild", nil)
	resp, _ := http.DefaultClient.Do(req)
	resp.Body.Close()
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("unauthenticated rebuild status=%d want 401", resp.StatusCode)
	}
	// With credentials -> 200.
	req, _ = http.NewRequest(http.MethodPost, h.srv.URL+"/v1/admin/rebuild", nil)
	req.SetBasicAuth("admin", "test-admin")
	resp, _ = http.DefaultClient.Do(req)
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("authenticated rebuild status=%d", resp.StatusCode)
	}
}

func TestHTTPHealth(t *testing.T) {
	h := newHarness(t, service.ConflictPriority)
	for _, p := range []string{"/healthz", "/readyz"} {
		if s, _ := h.get(t, p); s != 200 {
			t.Fatalf("%s status=%d want 200", p, s)
		}
	}
}

// Symbols containing "/" arrive percent-encoded in the path ("BTC%2FUSD");
// the handler must PathUnescape them, otherwise reads silently miss every row.
func TestHTTPEncodedSlashSymbol(t *testing.T) {
	h := newHarness(t, service.ConflictPriority)
	now := h.clock.now()
	base := time.Unix(now.Unix()-now.Unix()%60, 0).UTC()
	body := sampleBody("BTC/USD", base, 12345)
	if r, _ := h.signPost(t, "/v1/samples", "venueA", body,
		base.Add(time.Second), "slash-1"); r.StatusCode != 201 {
		t.Fatal("ingest encoded-slash symbol failed")
	}
	h.clock.set(base.Add(60 * time.Second))
	status, win := h.get(t, fmt.Sprintf("/v1/symbols/BTC%%2FUSD/windows/%d", base.Unix()))
	if status != 200 {
		t.Fatalf("read status=%d", status)
	}
	if win["twap"] != "12345.000000" {
		t.Fatalf("encoded symbol read twap=%v want 12345.000000", win["twap"])
	}
}

func TestHTTPBatchIngest(t *testing.T) {
	h := newHarness(t, service.ConflictPriority)
	now := h.clock.now()
	base := time.Unix(now.Unix()-now.Unix()%60, 0).UTC()
	// p=100 for 30s, p=200 for 30s -> TWAP 150.
	body, _ := json.Marshal(map[string]any{
		"samples": []map[string]any{
			{"symbol": "BCH", "ts_us": base.UnixMicro(), "price": 100},
			{"symbol": "BCH", "ts_us": base.Add(30 * time.Second).UnixMicro(), "price": 200},
		},
	})
	resp, out := h.signPost(t, "/v1/samples", "venueA", body,
		base.Add(time.Second), "batch-1")
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("batch status=%d body=%v", resp.StatusCode, out)
	}
	accepted, ok := out["accepted"].([]any)
	if !ok || len(accepted) != 2 {
		t.Fatalf("want 2 accepted results, got %v", out)
	}
	h.clock.set(base.Add(60 * time.Second))
	_, win := h.get(t, fmt.Sprintf("/v1/symbols/BCH/windows/%d", base.Unix()))
	if win["twap"] != "150.000000" {
		t.Fatalf("batch TWAP=%v want 150.000000", win["twap"])
	}
}

func af64(v any) float64 {
	switch x := v.(type) {
	case float64:
		return x
	default:
		return -1
	}
}
