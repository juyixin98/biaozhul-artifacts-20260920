package server

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"runtime"
	"testing"
	"time"

	"canceltree/internal/testutil"
)

type testServer struct {
	*httptest.Server
	s *Server
}

func newTestServer(t *testing.T) *testServer {
	t.Helper()
	s := New(Options{Addr: "127.0.0.1:0", DisableKeepAlives: true})
	ts := httptest.NewServer(s.Handler())
	t.Cleanup(func() {
		ts.CloseClientConnections()
		ts.Close()
	})
	return &testServer{Server: ts, s: s}
}

func (ts *testServer) process(t *testing.T, body any) (int, map[string]any) {
	t.Helper()
	raw, _ := json.Marshal(body)
	resp, err := http.Post(ts.URL+"/api/v1/process", "application/json", bytes.NewReader(raw))
	if err != nil {
		t.Fatalf("POST process: %v", err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	var out map[string]any
	_ = json.Unmarshal(data, &out)
	return resp.StatusCode, out
}

func (ts *testServer) processCtx(ctx context.Context, t *testing.T, body any) (*http.Response, error) {
	raw, _ := json.Marshal(body)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, ts.URL+"/api/v1/process", bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	return http.DefaultClient.Do(req)
}

func (ts *testServer) release(t *testing.T, id string) {
	t.Helper()
	resp, err := http.Post(ts.URL+"/api/v1/release?id="+id, "application/json", nil)
	if err != nil {
		t.Fatalf("release: %v", err)
	}
	_ = resp.Body.Close()
}

func (ts *testServer) diagnostics(t *testing.T) diagnostics {
	t.Helper()
	resp, err := http.Get(ts.URL + "/api/v1/diagnostics")
	if err != nil {
		t.Fatalf("diagnostics: %v", err)
	}
	defer resp.Body.Close()
	var d diagnostics
	if err := json.NewDecoder(resp.Body).Decode(&d); err != nil {
		t.Fatal(err)
	}
	return d
}

func (ts *testServer) waitUpstreamInflight(t *testing.T, n int64) {
	t.Helper()
	testutil.Eventually(t, 2*time.Second, func() bool {
		return ts.diagnostics(t).Upstream.Inflight >= n
	}, fmt.Sprintf("upstream inflight never reached %d", n))
}

func heldTaskDTO(id string, cleanup bool) map[string]any {
	t := map[string]any{
		"id": id,
		"call": map[string]any{
			"method": "GET",
			"url":    "/upstream/work?hold=1&hold_id=" + id,
		},
	}
	if cleanup {
		t["cleanup"] = map[string]any{"url": "/upstream/cleanup", "timeout_ms": 1000}
	}
	return t
}

func TestHealthz(t *testing.T) {
	ts := newTestServer(t)
	resp, err := http.Get(ts.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
}

func TestMethodGuards(t *testing.T) {
	ts := newTestServer(t)
	resp, err := http.Get(ts.URL + "/api/v1/process")
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("GET process status = %d, want 405", resp.StatusCode)
	}

	resp2, err := http.Get(ts.URL + "/api/v1/release?id=all")
	if err != nil {
		t.Fatal(err)
	}
	_ = resp2.Body.Close()
	if resp2.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("GET release status = %d, want 405", resp2.StatusCode)
	}
}

func TestReleaseEndpointReleasesHolds(t *testing.T) {
	ts := newTestServer(t)
	body := map[string]any{"tasks": []any{heldTaskDTO("via-api", false)}}
	done := make(chan int, 1)
	go func() {
		status, _ := ts.process(t, body)
		done <- status
	}()
	ts.waitUpstreamInflight(t, 1)
	// Release through the public API surface rather than the fake directly.
	resp, err := http.Post(ts.URL+"/api/v1/release?id=via-api", "application/json", nil)
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	select {
	case code := <-done:
		if code != http.StatusOK {
			t.Fatalf("status = %d", code)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("process did not finish after API release")
	}

	// Empty id releases everything.
	body2 := map[string]any{"tasks": []any{heldTaskDTO("bulk", false)}}
	done2 := make(chan int, 1)
	go func() {
		status, _ := ts.process(t, body2)
		done2 <- status
	}()
	ts.waitUpstreamInflight(t, 1)
	resp2, err := http.Post(ts.URL+"/api/v1/release", "application/json", nil)
	if err != nil {
		t.Fatal(err)
	}
	_ = resp2.Body.Close()
	select {
	case <-done2:
	case <-time.After(2 * time.Second):
		t.Fatal("process did not finish after bulk release")
	}
}

func TestProcessSuccessStructuredReport(t *testing.T) {
	ts := newTestServer(t)
	body := map[string]any{
		"request_id": "e2e-ok",
		"tasks": []any{
			map[string]any{"id": "a", "call": map[string]any{"url": "/upstream/work?delay=1ms"}},
			map[string]any{"id": "b", "call": map[string]any{"url": "/upstream/work?delay=2ms"}},
		},
	}
	status, out := ts.process(t, body)
	if status != http.StatusOK || out["status"] != "succeeded" {
		t.Fatalf("status=%d body=%v", status, out)
	}
	outs, _ := out["outcomes"].([]any)
	if len(outs) != 2 {
		t.Fatalf("outcomes = %v", outs)
	}
}

func TestProcessValidationErrors(t *testing.T) {
	ts := newTestServer(t)
	cases := []map[string]any{
		{"tasks": []any{}},
		{"tasks": []any{map[string]any{"id": "a", "call": map[string]any{}}}},
	}
	for i, body := range cases {
		raw, _ := json.Marshal(body)
		resp, err := http.Post(ts.URL+"/api/v1/process", "application/json", bytes.NewReader(raw))
		if err != nil {
			t.Fatal(err)
		}
		_ = resp.Body.Close()
		if resp.StatusCode != http.StatusBadRequest {
			t.Fatalf("case %d: status = %d, want 400", i, resp.StatusCode)
		}
	}

	// Duplicate ids.
	dup := map[string]any{"tasks": []any{
		map[string]any{"id": "x", "call": map[string]any{"url": "/upstream/work"}},
		map[string]any{"id": "x", "call": map[string]any{"url": "/upstream/work"}},
	}}
	status, out := ts.process(t, dup)
	if status != http.StatusBadRequest || out["error"] == nil {
		t.Fatalf("dup: status=%d body=%v", status, out)
	}

	// Malformed JSON.
	resp, err := http.Post(ts.URL+"/api/v1/process", "application/json", bytes.NewReader([]byte("{nope")))
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("malformed json status = %d", resp.StatusCode)
	}
}

func TestProcessFatalFailureCancelsTree(t *testing.T) {
	ts := newTestServer(t)
	body := map[string]any{
		"request_id": "e2e-fatal",
		"tasks": []any{
			heldTaskDTO("held-a", true),
			heldTaskDTO("held-b", true),
			map[string]any{
				"id":    "fatal",
				"fatal": true,
				"call":  map[string]any{"url": "/upstream/work?delay=40ms&fail=1&status=500"},
			},
		},
	}
	status, out := ts.process(t, body)
	if status != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503", status)
	}
	if out["status"] != "failed" || out["fatal_task_id"] != "fatal" {
		t.Fatalf("body = %v", out)
	}
	canceled := 0
	for _, o := range out["outcomes"].([]any) {
		om := o.(map[string]any)
		if om["id"] == "fatal" {
			if om["status"] != "failed" {
				t.Fatalf("fatal outcome = %v", om)
			}
			continue
		}
		if om["status"] != "canceled" || om["canceled_by"] != "fatal-sibling" {
			t.Fatalf("sibling outcome = %v", om)
		}
		if om["cleanup_ran"] != true {
			t.Fatalf("cleanup missing: %v", om)
		}
		canceled++
	}
	if canceled != 2 {
		t.Fatalf("canceled siblings = %d, want 2", canceled)
	}
	d := ts.diagnostics(t)
	if d.Upstream.Canceled < 2 || d.Upstream.Cleanups != 2 {
		t.Fatalf("upstream stats = %+v", d.Upstream)
	}
	if d.Upstream.Inflight != 0 || d.Connections.InFlight != 0 || d.Connections.Open != 0 {
		t.Fatalf("resources not drained: upstream=%+v conns=%+v", d.Upstream, d.Connections)
	}
}

func TestProcessReleaseAllowsCompletion(t *testing.T) {
	ts := newTestServer(t)
	body := map[string]any{"tasks": []any{heldTaskDTO("gated", false)}}
	done := make(chan int, 1)
	go func() {
		status, _ := ts.process(t, body)
		done <- status
	}()
	ts.waitUpstreamInflight(t, 1)
	select {
	case code := <-done:
		t.Fatalf("process returned early with %d", code)
	case <-time.After(50 * time.Millisecond):
	}
	ts.release(t, "gated")
	select {
	case code := <-done:
		if code != http.StatusOK {
			t.Fatalf("status = %d, want 200", code)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("process did not complete after release")
	}
}

func TestProcessClientDisconnectStopsWork(t *testing.T) {
	ts := newTestServer(t)
	body := map[string]any{
		"request_id": "e2e-disconnect",
		"tasks": []any{
			heldTaskDTO("d-a", true),
			heldTaskDTO("d-b", true),
			heldTaskDTO("d-c", true),
		},
	}
	ctx, cancel := context.WithCancel(context.Background())
	errCh := make(chan error, 1)
	go func() {
		resp, err := ts.processCtx(ctx, t, body)
		if err == nil {
			_ = resp.Body.Close()
		}
		errCh <- err
	}()
	ts.waitUpstreamInflight(t, 3)

	cancel()
	select {
	case err := <-errCh:
		if err == nil {
			t.Fatal("expected client-side error on disconnect")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("client request did not abort")
	}

	// Server side: despite the dead connection, cleanups must finish and
	// every upstream call / connection must drain.
	testutil.Eventually(t, 3*time.Second, func() bool {
		d := ts.diagnostics(t)
		return d.Upstream.Cleanups == 3 &&
			d.Upstream.Canceled >= 3 &&
			d.Upstream.Inflight == 0 &&
			d.Connections.InFlight == 0 &&
			d.Connections.Open == 0
	}, "server did not finish cleanups / drain resources after disconnect")
}

func TestProcessStallFaultTerminatedByDisconnect(t *testing.T) {
	ts := newTestServer(t)
	body := map[string]any{
		"tasks": []any{
			map[string]any{
				"id": "staller",
				"call": map[string]any{
					"url":   "/upstream/work",
					"fault": map[string]any{"kind": "stall"},
				},
				"cleanup": map[string]any{"url": "/upstream/cleanup"},
			},
		},
	}
	ctx, cancel := context.WithCancel(context.Background())
	errCh := make(chan error, 1)
	go func() {
		resp, err := ts.processCtx(ctx, t, body)
		if err == nil {
			_ = resp.Body.Close()
		}
		errCh <- err
	}()
	// Give the handler a moment to enter the stall, then disconnect.
	time.Sleep(50 * time.Millisecond)
	cancel()
	select {
	case err := <-errCh:
		if err == nil {
			t.Fatal("expected error after disconnecting a stalled task")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("stalled request was not terminated")
	}
	testutil.Eventually(t, 2*time.Second, func() bool {
		d := ts.diagnostics(t)
		return d.Upstream.Cleanups == 1 && d.Connections.InFlight == 0 && d.Connections.Open == 0
	}, "cleanup did not run / conns did not drain for stalled task")
}

func TestNoLeakAcrossRepeatedDisconnects(t *testing.T) {
	ts := newTestServer(t)
	// Warm up pools.
	ts.process(t, map[string]any{"tasks": []any{
		map[string]any{"id": "warm", "call": map[string]any{"url": "/upstream/work?delay=1ms"}},
	}})
	ts.Server.CloseClientConnections()
	runtime.GC()
	base := testutil.Snapshot()

	const iterations = 15
	for i := 0; i < iterations; i++ {
		body := map[string]any{"tasks": []any{
			heldTaskDTO(fmt.Sprintf("l-a-%d", i), true),
			heldTaskDTO(fmt.Sprintf("l-b-%d", i), true),
		}}
		ctx, cancel := context.WithCancel(context.Background())
		errCh := make(chan error, 1)
		go func() {
			resp, err := ts.processCtx(ctx, t, body)
			if err == nil {
				_ = resp.Body.Close()
			}
			errCh <- err
		}()
		ts.waitUpstreamInflight(t, 2)
		cancel()
		select {
		case <-errCh:
		case <-time.After(2 * time.Second):
			t.Fatalf("iteration %d: disconnect did not abort client", i)
		}
		// Let the server finish detached cleanups before the next round.
		testutil.Eventually(t, 2*time.Second, func() bool {
			d := ts.diagnostics(t)
			return d.Upstream.Inflight == 0 && d.Connections.InFlight == 0 && d.Connections.Open == 0
		}, fmt.Sprintf("iteration %d did not drain", i))
	}

	ts.Server.CloseClientConnections()
	want := testutil.Snapshot().Goroutines
	testutil.GoroutineSettled(t, want, 2*time.Second)
	final := testutil.Snapshot()
	d := ts.diagnostics(t)
	if d.Upstream.Inflight != 0 {
		t.Fatalf("inflight leaked: %+v", d.Upstream)
	}
	if d.Connections.InFlight != 0 || d.Connections.Open != 0 {
		t.Fatalf("connections leaked: %+v", d.Connections)
	}
	if final.Goroutines > base.Goroutines+2 {
		t.Fatalf("goroutine leak: base=%d final=%d", base.Goroutines, final.Goroutines)
	}
	if final.HeapAlloc > base.HeapAlloc+(2<<20) {
		t.Fatalf("heap grew beyond budget: base=%d final=%d", base.HeapAlloc, final.HeapAlloc)
	}
}
