package httpapi

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"tokenbudget/internal/budget"
	"tokenbudget/internal/clock"
	"tokenbudget/internal/rational"
)

const micro = budget.MicroPerToken

type harness struct {
	t   *testing.T
	srv *Server
	v   *clock.Virtual
	hs  *httptest.Server
}

func newHarness(t *testing.T, cfg budget.Config) *harness {
	t.Helper()
	v := clock.NewVirtual(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	sink := budget.NewMemorySink(1000)
	l, err := budget.New(cfg, v, sink)
	if err != nil {
		t.Fatalf("new limiter: %v", err)
	}
	sched := budget.NewScheduler(l, budget.InlineExecutor{}, sink)
	srv := NewServer(l, sched, sink, v, v)
	hs := httptest.NewServer(srv.Handler())
	t.Cleanup(hs.Close)
	return &harness{t: t, srv: srv, v: v, hs: hs}
}

func cfg() budget.Config {
	return budget.Config{
		Global:  budget.BucketConfig{Rate: rational.PerSecond(10), BurstMicro: 10 * micro},
		Default: budget.BucketConfig{Rate: rational.PerSecond(2), BurstMicro: 5 * micro},
	}
}

func (h *harness) post(path string, body string) (int, map[string]any) {
	return h.do(http.MethodPost, path, body)
}

func (h *harness) put(path string, body string) (int, map[string]any) {
	return h.do(http.MethodPut, path, body)
}

func (h *harness) do(method, path, body string) (int, map[string]any) {
	req, err := http.NewRequest(method, h.hs.URL+path, strings.NewReader(body))
	if err != nil {
		h.t.Fatalf("%s %s: %v", method, path, err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		h.t.Fatalf("%s %s: %v", method, path, err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func (h *harness) get(path string) (int, map[string]any) {
	resp, err := http.Get(h.hs.URL + path)
	if err != nil {
		h.t.Fatalf("GET %s: %v", path, err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func TestHealth(t *testing.T) {
	h := newHarness(t, cfg())
	code, body := h.get("/healthz")
	if code != 200 || body["status"] != "ok" {
		t.Fatalf("health code=%d body=%v", code, body)
	}
}

func TestAcquireBurstAndDeny(t *testing.T) {
	h := newHarness(t, cfg())
	// Tenant burst 5: 5 allowed, 6th 429.
	for i := 0; i < 5; i++ {
		code, body := h.post("/v1/acquire", `{"tenant":"a","cost":"1"}`)
		if code != 200 || body["allowed"] != true {
			t.Fatalf("acquire %d: code=%d body=%v", i+1, code, body)
		}
	}
	code, body := h.post("/v1/acquire", `{"tenant":"a","cost":"1"}`)
	if code != http.StatusTooManyRequests || body["allowed"] != false {
		t.Fatalf("6th acquire code=%d body=%v want 429 denied", code, body)
	}
	if body["reason"] != string(budget.DeniedTenant) {
		t.Fatalf("reason=%v", body["reason"])
	}

	// Advance 1s -> 2 more tokens.
	h.post("/internal/clock/advance", `{"ms":1000}`)
	for i := 0; i < 2; i++ {
		if code, body := h.post("/v1/acquire", `{"tenant":"a","cost":"1"}`); code != 200 || body["allowed"] != true {
			t.Fatalf("post-refill %d: code=%d body=%v", i+1, code, body)
		}
	}
	if code, _ := h.post("/v1/acquire", `{"tenant":"a","cost":"1"}`); code != 429 {
		t.Fatalf("third post-refill code=%d want 429", code)
	}
}

func TestGlobalDenyNoPartial(t *testing.T) {
	h := newHarness(t, cfg()) // global burst 10, tenant 5
	// Drain global using two tenants (5 each).
	h.post("/v1/acquire", `{"tenant":"a","cost":"1"}`)
	h.post("/v1/acquire", `{"tenant":"a","cost":"1"}`)
	h.post("/v1/acquire", `{"tenant":"a","cost":"1"}`)
	h.post("/v1/acquire", `{"tenant":"a","cost":"1"}`)
	h.post("/v1/acquire", `{"tenant":"a","cost":"1"}`)
	for i := 0; i < 5; i++ {
		if code, body := h.post("/v1/acquire", `{"tenant":"b","cost":"1"}`); code != 200 || body["allowed"] != true {
			t.Fatalf("b %d: %d %v", i+1, code, body)
		}
	}
	// Global now 0; fresh tenant c has 5 own tokens but must fail globally
	// and retain its full balance.
	code, body := h.post("/v1/acquire", `{"tenant":"c","cost":"1"}`)
	if code != 429 || body["reason"] != string(budget.DeniedGlobal) {
		t.Fatalf("global deny: code=%d body=%v", code, body)
	}
	_, state := h.get("/v1/buckets/c")
	tb := state["tenant_bucket"].(map[string]any)
	if tb["available"] != "5" {
		t.Fatalf("fresh tenant partially consumed: %v", tb["available"])
	}
}

func TestRejectsFloatCost(t *testing.T) {
	h := newHarness(t, cfg())
	// JSON number fractional is rejected by rate parser; cost uses string
	// parsing: a bare number 0.25 arrives as float64 — the handler expects a
	// string, so it fails cleanly.
	code, body := h.post("/v1/acquire", `{"tenant":"a","cost":0.25}`)
	if code != 400 {
		t.Fatalf("numeric fractional cost code=%d body=%v want 400", code, body)
	}
}

func TestFractionalCostExact(t *testing.T) {
	c := cfg()
	c.Global = budget.BucketConfig{Rate: rational.PerSecond(1), BurstMicro: 1 * micro}
	c.Default = budget.BucketConfig{Rate: rational.PerSecond(1), BurstMicro: 1 * micro}
	h := newHarness(t, c)
	for i := 0; i < 4; i++ {
		if code, body := h.post("/v1/acquire", `{"tenant":"a","cost":"0.25"}`); code != 200 || body["allowed"] != true {
			t.Fatalf("quarter %d: %d %v", i+1, code, body)
		}
	}
	if code, _ := h.post("/v1/acquire", `{"tenant":"a","cost":"0.25"}`); code != 429 {
		t.Fatalf("fifth quarter code=%d want 429", code)
	}
}

func TestConfigPutAndRateSwitch(t *testing.T) {
	h := newHarness(t, cfg())
	// Spend 5 from tenant a and global.
	for i := 0; i < 5; i++ {
		h.post("/v1/acquire", `{"tenant":"a","cost":"1"}`)
	}
	// Advance 1s: global refills 10 (back to cap 10), tenant a refills 2.
	h.post("/internal/clock/advance", `{"ms":1000}`)
	_, pre := h.get("/v1/buckets/a")
	if pre["tenant_bucket"].(map[string]any)["available"] != "2" {
		t.Fatalf("pre-switch tenant=%v want 2", pre["tenant_bucket"])
	}
	// Raise rate/burst: balances must be preserved, not topped up.
	newCfg := `{
	  "global":  {"rate":"100","burst":"20"},
	  "default": {"rate":"100","burst":"20"}
	}`
	if code, body := h.put("/v1/config", newCfg); code != 200 {
		t.Fatalf("put config: %d %v", code, body)
	}
	_, state := h.get("/v1/buckets/a")
	if state["global"].(map[string]any)["available"] != "10" {
		t.Fatalf("global after switch=%v want 10 (preserved at cap, no stock minted)", state["global"])
	}
	if state["tenant_bucket"].(map[string]any)["available"] != "2" {
		t.Fatalf("tenant after switch=%v want 2 (preserved, not topped up)", state["tenant_bucket"])
	}
	// Subsequent refill uses the new rate: 10ms -> exactly 1 more token.
	h.post("/internal/clock/advance", `{"ms":10}`)
	_, after := h.get("/v1/buckets/a")
	if after["tenant_bucket"].(map[string]any)["available"] != "3" {
		t.Fatalf("post-switch refill tenant=%v want 3", after["tenant_bucket"])
	}
	// Exact fractional rate via string.
	third := `{
	  "global":  {"rate":"1/3","burst":"3"},
	  "default": {"rate":"1/3","burst":"3"}
	}`
	if code, body := h.put("/v1/config", third); code != 200 {
		t.Fatalf("1/3 config rejected: %d %v", code, body)
	}
}

func TestConcurrentAcquire(t *testing.T) {
	c := cfg()
	c.Global = budget.BucketConfig{Rate: rational.PerSecond(50), BurstMicro: 50 * micro}
	c.Default = budget.BucketConfig{Rate: rational.PerSecond(50), BurstMicro: 50 * micro}
	h := newHarness(t, c)

	const n = 200
	var wg sync.WaitGroup
	var mu sync.Mutex
	allowed := 0
	statuses := make([]int, n)
	wg.Add(n)
	for i := 0; i < n; i++ {
		i := i
		go func() {
			defer wg.Done()
			code, _ := h.post("/v1/acquire", `{"tenant":"shared","cost":"1"}`)
			mu.Lock()
			statuses[i] = code
			if code == 200 {
				allowed++
			}
			mu.Unlock()
		}()
	}
	wg.Wait()
	if allowed != 50 {
		t.Fatalf("allowed=%d want exactly 50", allowed)
	}
	ok200, ok429 := 0, 0
	for _, c := range statuses {
		if c == 200 {
			ok200++
		} else if c == 429 {
			ok429++
		} else {
			t.Fatalf("unexpected status %d", c)
		}
	}
	if ok200+ok429 != n {
		t.Fatal("status accounting")
	}
}

func TestEventsEndpoint(t *testing.T) {
	h := newHarness(t, cfg())
	h.post("/v1/acquire", `{"tenant":"a","cost":"1"}`)
	h.post("/v1/acquire", `{"tenant":"a","cost":"1"}`)
	h.post("/v1/acquire", `{"tenant":"a","cost":"1"}`)
	h.post("/v1/acquire", `{"tenant":"a","cost":"1"}`)
	h.post("/v1/acquire", `{"tenant":"a","cost":"1"}`)
	h.post("/v1/acquire", `{"tenant":"a","cost":"1"}`) // denied

	code, body := h.get("/v1/events?tenant=a&limit=2")
	if code != 200 {
		t.Fatalf("events code=%d", code)
	}
	evs := body["events"].([]any)
	if len(evs) != 2 {
		t.Fatalf("limited events=%d want 2", len(evs))
	}
	last := evs[1].(map[string]any)
	if last["result"] != string(budget.DeniedTenant) {
		t.Fatalf("last event=%v want denied_tenant", last["result"])
	}
	if last["tenant_bucket"].(map[string]any)["available"] != "0" {
		t.Fatal("denied event must still carry snapshot")
	}
}

func TestClockAdvanceEndpoint(t *testing.T) {
	h := newHarness(t, cfg())
	code, body := h.post("/internal/clock/advance", `{"ns":100}`)
	if code != 200 {
		t.Fatalf("advance: %d %v", code, body)
	}
	if !bytes.Contains([]byte(body["now"].(string)), []byte("2026")) {
		t.Fatalf("now=%v", body["now"])
	}
}
