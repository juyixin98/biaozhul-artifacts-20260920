package client

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"etagrace/internal/clock"
	"etagrace/internal/notifier"
	"etagrace/internal/server"
	"etagrace/internal/store"
)

func TestNewAppliesDefaults(t *testing.T) {
	// nil http client and nil policy must not panic and still function.
	c := New("http://127.0.0.1:1/", nil, nil)
	if c.base != "http://127.0.0.1:1" {
		t.Fatalf("base trim: %q", c.base)
	}
	// Default policy sleeper is time.Sleep; zero latency means no wait.
	_ = NewFailurePolicy(0, 0, nil)
}

func TestClassifyAndErrorStatuses(t *testing.T) {
	ts, clk := newService(t)
	c := New(ts.URL, ts.Client(), NewFailurePolicy(0, 0, clk.Sleep))
	ctx := context.Background()

	// GET missing -> not_found.
	missing, err := c.Get(ctx, "nope")
	if err != nil || missing.Outcome != "not_found" || missing.StatusCode != http.StatusNotFound {
		t.Fatalf("missing: %+v err=%v", missing.Attempt, err)
	}

	// Create then a malformed If-Match -> server 400 -> http_error outcome.
	if _, err := c.Create(ctx, "doc", []byte("a")); err != nil {
		t.Fatal(err)
	}
	bad, err := c.Update(ctx, "doc", "not-a-quoted-tag", []byte("b"))
	if err != nil || bad.Outcome != "http_error" || bad.StatusCode != http.StatusBadRequest {
		t.Fatalf("malformed tag: %+v err=%v", bad.Attempt, err)
	}
}

func TestUpdateWithRetryGetNonOKAndMutateError(t *testing.T) {
	ts, clk := newService(t)
	c := New(ts.URL, ts.Client(), NewFailurePolicy(0, 0, clk.Sleep))
	ctx := context.Background()

	// GET returns 404: loop ends without success and reports that status.
	rep, err := c.UpdateWithRetry(ctx, "ghost", 3, func([]byte) ([]byte, error) {
		t.Fatal("mutate must not run when GET fails")
		return nil, nil
	})
	if err != nil || rep.Success || rep.FinalStatus != http.StatusNotFound {
		t.Fatalf("missing resource: rep=%+v err=%v", rep, err)
	}

	if _, err := c.Create(ctx, "doc", []byte("a")); err != nil {
		t.Fatal(err)
	}
	mutErr := errors.New("cannot derive body")
	rep2, err := c.UpdateWithRetry(ctx, "doc", 3, func([]byte) ([]byte, error) {
		return nil, mutErr
	})
	if !errors.Is(err, mutErr) {
		t.Fatalf("want mutate error propagated, got rep=%+v err=%v", rep2, err)
	}
}

// Ensure the client works against the fully wired server with retries enabled
// (integration smoke with a short real timeout).
func TestAgainstWiredServer(t *testing.T) {
	clk := clock.NewFake(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	audit := notifier.NewAuditLog()
	faulty := notifier.NewFaulty(audit, clk)
	faulty.ArmFailures(1) // first audit attempt fails, server retries
	notif := server.RetryingNotifier{Inner: faulty, Clk: clk, Attempts: 3, BaseBackoff: time.Millisecond}
	st := store.New(clk)
	ts := httptest.NewServer(server.New(st, clk, notif, audit).Handler())
	defer ts.Close()

	c := New(ts.URL, ts.Client(), NewFailurePolicy(0, 0, clk.Sleep))
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	created, err := c.Create(ctx, "doc", []byte("a"))
	if err != nil || created.StatusCode != http.StatusCreated {
		t.Fatalf("create through one injected fault: %+v err=%v", created, err)
	}
}
