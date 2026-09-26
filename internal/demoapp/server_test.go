package demoapp_test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"cbhalfopen/internal/breaker"
	"cbhalfopen/internal/demoapp"
	"cbhalfopen/internal/faultclient"
)

type testClient struct {
	t   *testing.T
	srv *httptest.Server
	hc  *http.Client
}

func newTestClient(t *testing.T) *testClient {
	t.Helper()
	srv := httptest.NewServer(demoapp.New(testBreakerCfg(), faultclient.Config{}).Handler())
	t.Cleanup(srv.Close)
	return &testClient{t: t, srv: srv, hc: &http.Client{Timeout: 5 * time.Second}}
}

func testBreakerCfg() breaker.Config {
	return breaker.Config{
		SlidingWindowSize: 10,
		MinRequests:       5,
		FailureThreshold:  0.5,
		OpenCooldown:      5 * time.Second,
		HalfOpenMaxProbes: 3,
		RequiredSuccesses: 2,
	}
}

func (c *testClient) postJSON(path string, body any) map[string]any {
	c.t.Helper()
	var buf bytes.Buffer
	if err := json.NewEncoder(&buf).Encode(body); err != nil {
		c.t.Fatalf("encode: %v", err)
	}
	resp, err := c.hc.Post(c.srv.URL+path, "application/json", &buf)
	if err != nil {
		c.t.Fatalf("POST %s: %v", path, err)
	}
	defer resp.Body.Close()
	return c.decode(resp)
}

func (c *testClient) postEmpty(path string) map[string]any {
	c.t.Helper()
	resp, err := c.hc.Post(c.srv.URL+path, "application/json", nil)
	if err != nil {
		c.t.Fatalf("POST %s: %v", path, err)
	}
	defer resp.Body.Close()
	return c.decode(resp)
}

func (c *testClient) get(path string) map[string]any {
	c.t.Helper()
	resp, err := c.hc.Get(c.srv.URL + path)
	if err != nil {
		c.t.Fatalf("GET %s: %v", path, err)
	}
	defer resp.Body.Close()
	return c.decode(resp)
}

func (c *testClient) decode(resp *http.Response) map[string]any {
	c.t.Helper()
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		c.t.Fatalf("decode %d: %v", resp.StatusCode, err)
	}
	return out
}

func (c *testClient) state() map[string]any {
	return c.get("/state")
}

func (c *testClient) advance(ms int64) {
	out := c.postJSON("/clock/advance", map[string]any{"ms": ms})
	if out["timers_fired"] == nil {
		c.t.Fatalf("advance response missing timers_fired: %v", out)
	}
}

func (c *testClient) setMode(mode string) {
	c.postJSON("/upstream/mode", map[string]any{"mode": mode})
}

func (c *testClient) call() map[string]any {
	return c.postEmpty("/call")
}

func (c *testClient) stateString() string {
	s, _ := c.state()["state"].(string)
	return s
}

func TestInitialStateClosed(t *testing.T) {
	c := newTestClient(t)
	if got := c.stateString(); got != string(breaker.StateClosed) {
		t.Fatalf("state = %s", got)
	}
	if h := c.get("/healthz"); h["ok"] != true {
		t.Fatalf("health = %v", h["ok"])
	}
}

func TestFullLifecycleOverHTTP(t *testing.T) {
	c := newTestClient(t)

	// Fail five calls: breaker trips.
	c.setMode("fail")
	for i := 0; i < 5; i++ {
		out := c.call()
		result := out["result"].(map[string]any)
		if result["outcome"] != string(breaker.OutcomeFailure) {
			t.Fatalf("call %d outcome = %v", i, result["outcome"])
		}
	}
	if got := c.stateString(); got != string(breaker.StateOpen) {
		t.Fatalf("state after failures = %s", got)
	}

	// Calls during open are rejected at the breaker.
	out := c.call()
	if out["rejected"] == nil || out["rejected"] == "" {
		t.Fatalf("open call not rejected: %v", out)
	}

	// Cooldown passes virtually; two successful probes close the breaker.
	c.advance(5000)
	c.setMode("ok")
	probe1 := c.call()
	if got := c.stateString(); got != string(breaker.StateHalfOpen) {
		t.Fatalf("after first probe state = %s", got)
	}
	if r := probe1["result"].(map[string]any); r["probe"] != true {
		t.Fatalf("first call after cooldown not marked probe: %v", r)
	}
	c.call()
	if got := c.stateString(); got != string(breaker.StateClosed) {
		t.Fatalf("after second probe state = %s", got)
	}

	// Healthy traffic resumes.
	c.call()
	snap := c.state()
	if snap["window_failures"].(float64) != 0 {
		t.Fatalf("window failures = %v", snap["window_failures"])
	}
}

func TestClientFaultInjectionOverHTTP(t *testing.T) {
	c := newTestClient(t)
	c.postJSON("/client/config", map[string]any{"fail_next_n": 2})
	for i := 0; i < 2; i++ {
		out := c.call()
		result := out["result"].(map[string]any)
		if result["reason"] != "injected_transport_error" {
			t.Fatalf("call %d reason = %v", i, result["reason"])
		}
	}
	out := c.call()
	if result := out["result"].(map[string]any); result["outcome"] != string(breaker.OutcomeSuccess) {
		t.Fatalf("third call = %v", result)
	}
}

func TestVirtualTimeoutOverHTTP(t *testing.T) {
	c := newTestClient(t)
	c.setMode("hang")
	c.postJSON("/client/config", map[string]any{"timeout_ms": 100})

	done := make(chan map[string]any, 1)
	go func() {
		done <- c.call()
	}()
	c.waitForPending(1)

	c.advance(100)
	var out map[string]any
	select {
	case out = <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("timed-out HTTP call never returned")
	}
	result := out["result"].(map[string]any)
	if result["outcome"] != string(breaker.OutcomeFailure) || result["reason"] != "timeout" {
		t.Fatalf("result = %v", result)
	}
	// Clean up the parked upstream attempt.
	c.postJSON("/upstream/release", map[string]any{"mode": "fail", "all": true})
}

func TestLateFailureOverHTTPStaysStale(t *testing.T) {
	c := newTestClient(t)

	// An old call hangs while the breaker trips and recovers.
	c.setMode("hang")
	asyncOut := c.postEmpty("/call/async")
	id := uint64(asyncOut["id"].(float64))
	c.waitForPending(1)

	c.setMode("fail")
	for i := 0; i < 5; i++ {
		c.call()
	}
	if got := c.stateString(); got != string(breaker.StateOpen) {
		t.Fatalf("state = %s", got)
	}
	c.advance(5000)
	c.setMode("ok")
	c.call()
	c.call()
	if got := c.stateString(); got != string(breaker.StateClosed) {
		t.Fatalf("state after probes = %s", got)
	}

	// Release the original call as a failure after recovery.
	c.postJSON("/upstream/release", map[string]any{"mode": "fail", "id": id})

	res := c.waitForCallDone(id)
	if res["outcome"] != string(breaker.OutcomeFailure) {
		t.Fatalf("late call outcome = %v", res["outcome"])
	}
	snap := c.state()
	if snap["state"] != string(breaker.StateClosed) {
		t.Fatalf("state = %v, want closed", snap["state"])
	}
	counters := snap["counters"].(map[string]any)
	if counters["stale_results"].(float64) != 1 {
		t.Fatalf("stale_results = %v, want 1", counters["stale_results"])
	}
}

func TestCanceledCallOverHTTPNotAFailure(t *testing.T) {
	c := newTestClient(t)
	c.setMode("hang")

	out := c.postEmpty("/call/async")
	id := uint64(out["id"].(float64))
	c.waitForPending(1)

	c.postJSON(fmt.Sprintf("/call/%d/cancel", id), map[string]any{})
	res := c.waitForCallDone(id)
	if res["outcome"] != string(breaker.OutcomeCanceled) {
		t.Fatalf("canceled outcome = %v", res["outcome"])
	}
	snap := c.state()
	if snap["state"] != string(breaker.StateClosed) {
		t.Fatalf("state = %v", snap["state"])
	}
	counters := snap["counters"].(map[string]any)
	if counters["failures"].(float64) != 0 {
		t.Fatalf("failures = %v, want 0", counters["failures"])
	}
	if counters["canceled"].(float64) != 1 {
		t.Fatalf("canceled = %v, want 1", counters["canceled"])
	}
	// Release the server-side parked attempt so the server can shut down.
	c.postJSON("/upstream/release", map[string]any{"mode": "fail", "all": true})
}

func TestResetEndpoint(t *testing.T) {
	c := newTestClient(t)
	c.setMode("fail")
	for i := 0; i < 5; i++ {
		c.call()
	}
	if got := c.stateString(); got != string(breaker.StateOpen) {
		t.Fatalf("state = %s", got)
	}
	out := c.postEmpty("/reset")
	if out["state"] != string(breaker.StateClosed) {
		t.Fatalf("reset state = %v", out["state"])
	}
}

func (c *testClient) waitForPending(n int) {
	c.t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		out := c.get("/upstream/pending")
		if list, ok := out["pending"].([]any); ok && len(list) >= n {
			return
		}
	}
	c.t.Fatalf("only %d calls parked", n)
}

func (c *testClient) waitForCallDone(id uint64) map[string]any {
	c.t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		out := c.get("/calls")
		for _, item := range out["calls"].([]any) {
			call := item.(map[string]any)
			if uint64(call["id"].(float64)) == id {
				if done, _ := call["done"].(bool); done {
					if call["result"] == nil {
						return map[string]any{"reason": call["reason"]}
					}
					return call["result"].(map[string]any)
				}
			}
		}
		time.Sleep(10 * time.Millisecond)
	}
	c.t.Fatalf("call %d never finished", id)
	return nil
}
