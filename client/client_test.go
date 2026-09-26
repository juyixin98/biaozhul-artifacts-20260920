package client

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"etagrace/internal/clock"
	"etagrace/internal/notifier"
	"etagrace/internal/server"
	"etagrace/internal/store"
)

func newService(t *testing.T) (*httptest.Server, *clock.Fake) {
	t.Helper()
	clk := clock.NewFake(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	audit := notifier.NewAuditLog()
	st := store.New(clk)
	ts := httptest.NewServer(server.New(st, clk, notifier.NewReliable(audit), audit).Handler())
	t.Cleanup(ts.Close)
	return ts, clk
}

// failingPUTTransport injects 412 responses for the first n PUT requests,
// deterministically simulating "another writer won the race" without relying on
// goroutine scheduling. All other requests pass through unchanged.
type failingPUTTransport struct {
	base      http.RoundTripper
	remaining int // -1 = fail every PUT
}

func (f *failingPUTTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	if req.Method == http.MethodPut && (f.remaining != 0) {
		if f.remaining > 0 {
			f.remaining--
		}
		body := io.NopCloser(strings.NewReader(`{"error":"precondition_failed","message":"injected 412"}`))
		return &http.Response{
			StatusCode: http.StatusPreconditionFailed,
			Header:     http.Header{"Content-Type": []string{"application/json"}},
			Body:       body,
			Request:    req,
		}, nil
	}
	return f.base.RoundTrip(req)
}

func TestUpdateWithRetryWinsAfterLosingRace(t *testing.T) {
	ts, clk := newService(t)
	hc := &http.Client{Transport: &failingPUTTransport{base: ts.Client().Transport, remaining: 1}}
	c := New(ts.URL, hc, NewFailurePolicy(0, 0, clk.Sleep))
	ctx := context.Background()

	if _, err := c.Create(ctx, "doc", []byte("v0")); err != nil {
		t.Fatal(err)
	}

	rep, err := c.UpdateWithRetry(ctx, "doc", 5, func(cur []byte) ([]byte, error) {
		return append(bytes.Clone(cur), []byte("+us")...), nil
	})
	if err != nil {
		t.Fatalf("cas: %v", err)
	}
	if !rep.Success {
		t.Fatalf("CAS should converge, report=%+v", rep)
	}
	if rep.Retries != 1 {
		t.Fatalf("expected exactly one retry (one injected 412 then win), got %d", rep.Retries)
	}
	outcomes := make([]string, len(rep.Attempts))
	for i, a := range rep.Attempts {
		outcomes[i] = a.Outcome
	}
	want := []string{"ok", "precondition_failed", "ok", "ok"}
	if len(outcomes) != len(want) {
		t.Fatalf("attempts=%v want=%v", outcomes, want)
	}
	for i := range want {
		if outcomes[i] != want[i] {
			t.Fatalf("attempt %d: %s want %s (all=%v)", i, outcomes[i], want[i], outcomes)
		}
	}
}

func TestUpdateWithRetryGivesUpAfterMaxAttempts(t *testing.T) {
	ts, clk := newService(t)
	hc := &http.Client{Transport: &failingPUTTransport{base: ts.Client().Transport, remaining: -1}}
	c := New(ts.URL, hc, NewFailurePolicy(0, 0, clk.Sleep))
	ctx := context.Background()
	if _, err := c.Create(ctx, "doc", []byte("v0")); err != nil {
		t.Fatal(err)
	}

	rep, err := c.UpdateWithRetry(ctx, "doc", 3, func(cur []byte) ([]byte, error) {
		return []byte("loser"), nil
	})
	if err != nil {
		t.Fatalf("unexpected transport error: %v", err)
	}
	if rep.Success {
		t.Fatal("CAS must report failure when every PUT loses")
	}
	if rep.FinalStatus != http.StatusPreconditionFailed {
		t.Fatalf("final status=%d", rep.FinalStatus)
	}
	if len(rep.Attempts) != 6 { // 3 x (GET + PUT)
		t.Fatalf("want 6 attempts, got %d", len(rep.Attempts))
	}
}

func TestTransportFaultInjection(t *testing.T) {
	ts, clk := newService(t)
	c := New(ts.URL, ts.Client(), NewFailurePolicy(1, 0, clk.Sleep))
	ctx := context.Background()

	res, err := c.Create(ctx, "doc", []byte("v0"))
	if err == nil || res.Outcome != "transport_error" {
		t.Fatalf("first call should be injected failure, got %+v err=%v", res, err)
	}
	res2, err := c.Create(ctx, "doc", []byte("v0"))
	if err != nil || res2.StatusCode != http.StatusCreated {
		t.Fatalf("second create should succeed, got %+v err=%v", res2, err)
	}
}

func TestBasicResourceFlowAndDelete(t *testing.T) {
	ts, clk := newService(t)
	c := New(ts.URL, ts.Client(), NewFailurePolicy(0, 0, clk.Sleep))
	ctx := context.Background()

	created, err := c.Create(ctx, "doc", []byte("a"))
	if err != nil {
		t.Fatal(err)
	}
	got, err := c.Get(ctx, "doc")
	if err != nil || got.StatusCode != http.StatusOK || string(got.Body) != "a" || got.ETag != created.ETag {
		t.Fatalf("get: %+v body=%q err=%v", got.Attempt, got.Body, err)
	}
	put, err := c.Update(ctx, "doc", created.ETag, []byte("b"))
	if err != nil || put.StatusCode != http.StatusOK {
		t.Fatalf("update: %+v err=%v", put.Attempt, err)
	}
	del, err := c.Delete(ctx, "doc", put.ETag)
	if err != nil || del.StatusCode != http.StatusNoContent {
		t.Fatalf("delete: %+v err=%v", del.Attempt, err)
	}
	missing, _ := c.Get(ctx, "doc")
	if missing.StatusCode != http.StatusNotFound {
		t.Fatalf("get after delete: %d", missing.StatusCode)
	}
}

func TestVersionFromTag(t *testing.T) {
	cases := []struct {
		in   string
		want int64
		err  bool
	}{
		{`"v7-abcdef"`, 7, false},
		{`W/"v12-deadbeef"`, 12, false},
		{`"abc"`, 0, true},
	}
	for _, tc := range cases {
		v, err := VersionFromTag(tc.in)
		if tc.err {
			if err == nil {
				t.Errorf("%s: expected error", tc.in)
			}
			continue
		}
		if err != nil || v != tc.want {
			t.Errorf("%s: got %d, %v want %d", tc.in, v, err, tc.want)
		}
	}
}
