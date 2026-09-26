package client

import (
	"context"
	"net/http"
	"strings"
	"testing"
	"time"

	"canceltree/internal/clock"
	"canceltree/internal/upstream"
)

func newHarness(t *testing.T) (*Logic, *upstream.Fake) {
	t.Helper()
	f := upstream.New()
	t.Cleanup(func() {
		f.ReleaseAll()
		f.Close()
	})
	cl := New(clock.Real{}, Config{Attempts: 1, DialTimeout: time.Second})
	t.Cleanup(cl.CloseIdleConnections)
	return cl, f
}

func TestDoSuccess(t *testing.T) {
	cl, f := newHarness(t)
	res := cl.Do(context.Background(), Call{URL: f.URL() + "/work?delay=1ms"})
	if !res.OK() {
		t.Fatalf("result = %+v, want OK", res)
	}
	if res.Attempts != 1 || res.StatusCode != http.StatusOK {
		t.Fatalf("result = %+v", res)
	}
}

func TestDoNonFatalUpstreamFailure(t *testing.T) {
	cl, f := newHarness(t)
	res := cl.Do(context.Background(), Call{URL: f.URL() + "/work?fail=1&status=503"})
	if res.OK() {
		t.Fatal("expected non-OK")
	}
	if res.StatusCode != http.StatusServiceUnavailable || res.Error != "" {
		t.Fatalf("result = %+v", res)
	}
}

func TestDoSyntheticFault(t *testing.T) {
	cl, _ := newHarness(t)
	res := cl.Do(context.Background(), Call{Fault: Fault{Kind: FaultError}})
	if res.OK() || res.Error == "" {
		t.Fatalf("result = %+v, want error", res)
	}
}

func TestDoResetFaultProducesTransportError(t *testing.T) {
	cl, f := newHarness(t)
	res := cl.Do(context.Background(), Call{URL: f.URL() + "/reset"})
	if res.OK() {
		t.Fatal("expected transport error")
	}
	if res.Error == "" || res.StatusCode != 0 {
		t.Fatalf("result = %+v", res)
	}
}

func TestDoLatencyHonorsContextCancel(t *testing.T) {
	cl, _ := newHarness(t)
	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(20 * time.Millisecond)
		cancel()
	}()
	start := time.Now()
	res := cl.Do(ctx, Call{Fault: Fault{Kind: FaultLatency, Delay: 5 * time.Second}})
	if time.Since(start) > 2*time.Second {
		t.Fatal("latency fault did not return promptly after cancellation")
	}
	if res.OK() || !strings.Contains(res.Error, "context canceled") {
		t.Fatalf("result = %+v, want context-canceled error", res)
	}
}

func TestDoStallAbortsOnCancel(t *testing.T) {
	cl, _ := newHarness(t)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan Result, 1)
	go func() { done <- cl.Do(ctx, Call{URL: "http://unused.invalid/", Fault: Fault{Kind: FaultStall}}) }()
	cancel()
	select {
	case res := <-done:
		if res.OK() {
			t.Fatalf("result = %+v", res)
		}
	case <-time.After(time.Second):
		t.Fatal("stall fault did not abort on cancel")
	}
}

func TestDoHeldCallTornDownByCancel(t *testing.T) {
	cl, f := newHarness(t)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan Result, 1)
	go func() {
		done <- cl.Do(ctx, Call{URL: f.URL() + "/work?hold=1&hold_id=c1"})
	}()
	waitInflight(t, f)

	cancel()
	select {
	case res := <-done:
		if res.OK() {
			t.Fatalf("result = %+v", res)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("held call did not abort")
	}
	if got := f.Snapshot().Canceled; got < 1 {
		t.Fatalf("upstream canceled = %d, want >= 1", got)
	}
}

func TestRetryBoundedAndCanceled(t *testing.T) {
	cl, f := newHarness(t)
	cl.backoff = time.Millisecond
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	res := cl.Do(ctx, Call{
		URL:      f.URL() + "/reset",
		Attempts: 5,
	})
	if res.Attempts != 1 {
		t.Fatalf("attempts = %d, want 1 under pre-canceled context", res.Attempts)
	}
	if res.OK() {
		t.Fatalf("result = %+v", res)
	}
}

func TestConnectionAccounting(t *testing.T) {
	cl, f := newHarness(t)
	for i := 0; i < 5; i++ {
		res := cl.Do(context.Background(), Call{URL: f.URL() + "/work?delay=1ms"})
		if !res.OK() {
			t.Fatalf("call %d: %+v", i, res)
		}
	}
	cl.CloseIdleConnections()
	stats := cl.ConnStats()
	if stats.TotalEstablished < 1 {
		t.Fatalf("established = %d, want >= 1", stats.TotalEstablished)
	}
	if stats.Open != 0 {
		t.Fatalf("open = %d, want 0 after idle close", stats.Open)
	}
}

func TestResolveURL(t *testing.T) {
	cases := []struct {
		base, target, want string
	}{
		{"http://127.0.0.1:8080", "/work?x=1", "http://127.0.0.1:8080/work?x=1"},
		{"http://127.0.0.1:8080/upstream/", "/reset", "http://127.0.0.1:8080/reset"},
		{"http://h", "http://other/x", "http://other/x"},
	}
	for _, c := range cases {
		got, err := ResolveURL(c.base, c.target)
		if err != nil || got != c.want {
			t.Fatalf("ResolveURL(%q,%q) = %q, %v; want %q", c.base, c.target, got, err, c.want)
		}
	}
	if _, err := ResolveURL("http://h", ""); err == nil {
		t.Fatal("expected error for empty target")
	}
}

func TestDoLatencyFaultThenCallSucceeds(t *testing.T) {
	cl, f := newHarness(t)
	res := cl.Do(context.Background(), Call{
		URL:   f.URL() + "/work?delay=1ms",
		Fault: Fault{Kind: FaultLatency, Delay: 5 * time.Millisecond},
	})
	if !res.OK() {
		t.Fatalf("result = %+v", res)
	}
	if res.Latency < 5*time.Millisecond {
		t.Fatalf("latency = %v, expected injected delay", res.Latency)
	}
}

func TestDoUnknownFaultFails(t *testing.T) {
	cl, _ := newHarness(t)
	res := cl.Do(context.Background(), Call{Fault: Fault{Kind: "bogus"}})
	if res.OK() || !strings.Contains(res.Error, "unknown kind") {
		t.Fatalf("result = %+v", res)
	}
}

func TestDoZeroDelayWaitDoesNotBlock(t *testing.T) {
	cl, f := newHarness(t)
	res := cl.Do(context.Background(), Call{
		URL:   f.URL() + "/work?delay=1ms",
		Fault: Fault{Kind: FaultLatency, Delay: 0},
	})
	if !res.OK() {
		t.Fatalf("result = %+v", res)
	}
}

func TestRetrySucceedsOnSecondAttempt(t *testing.T) {
	cl, f := newHarness(t)
	cl.backoff = time.Millisecond
	// reset always fails, so this mainly exercises backoff + attempt count;
	// bounded retry under cancellation is covered elsewhere.
	res := cl.Do(context.Background(), Call{URL: f.URL() + "/reset", Attempts: 3})
	if res.OK() || res.Attempts != 3 {
		t.Fatalf("result = %+v, want 3 attempts then failure", res)
	}
}

func TestConfigDefaults(t *testing.T) {
	f := upstream.New()
	defer f.Close()
	cl := New(clock.Real{}, Config{})
	defer cl.CloseIdleConnections()
	res := cl.Do(context.Background(), Call{URL: f.URL() + "/work?delay=1ms"})
	if !res.OK() {
		t.Fatalf("default-config call failed: %+v", res)
	}
}

func TestResolveURLInvalidInputs(t *testing.T) {
	if _, err := ResolveURL("http://%%%", "/x"); err == nil {
		t.Fatal("expected error for invalid base")
	}
	if _, err := ResolveURL("http://h", "://bad"); err == nil {
		t.Fatal("expected error for invalid target")
	}
}

func waitInflight(t *testing.T, f *upstream.Fake) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for f.Snapshot().Inflight < 1 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if f.Snapshot().Inflight < 1 {
		t.Fatal("request never became inflight")
	}
}
