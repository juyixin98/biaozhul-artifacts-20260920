package executor

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
	"time"

	"agingqueue/queue"
)

func TestRegistryDispatchAndUnknownType(t *testing.T) {
	r := NewRegistry()
	j := &queue.Job{ID: "1", Type: "noop"}
	if err := r.Execute(context.Background(), j); err != nil {
		t.Fatalf("noop: %v", err)
	}

	bad := &queue.Job{ID: "2", Type: "nope"}
	err := r.Execute(context.Background(), bad)
	if !errors.Is(err, ErrUnknownType) {
		t.Fatalf("unknown type err = %v, want ErrUnknownType", err)
	}
	if !queue.IsFatal(err) {
		t.Fatal("unknown-type dispatch must be fatal (config error, not retried)")
	}
}

func TestFatalAlias(t *testing.T) {
	base := errors.New("bad input")
	err := Fatal(base)
	if !IsFatal(err) {
		t.Fatal("IsFatal should detect executor.Fatal")
	}
	if !errors.Is(err, base) {
		t.Fatal("Fatal should wrap the original error")
	}
	var fe *queue.FatalError
	if !errors.As(err, &fe) {
		t.Fatal("Fatal should produce *queue.FatalError")
	}
	if Fatal(nil) != nil {
		t.Fatal("Fatal(nil) must be nil")
	}
}

func TestEchoDelayAndBadPayload(t *testing.T) {
	r := NewRegistry()

	// Bad payload is fatal: retrying cannot fix malformed JSON.
	j := &queue.Job{ID: "1", Type: "echo", Payload: json.RawMessage(`{`)}
	if err := r.Execute(context.Background(), j); !queue.IsFatal(err) {
		t.Fatalf("bad payload err = %v, want fatal", err)
	}

	// A short delay is honored and canceled via context.
	j2 := &queue.Job{
		ID:      "2",
		Type:    "echo",
		Payload: json.RawMessage(`{"delay":"50ms"}`),
	}
	start := time.Now()
	if err := r.Execute(context.Background(), j2); err != nil {
		t.Fatalf("echo delayed: %v", err)
	}
	if time.Since(start) < 40*time.Millisecond {
		t.Fatal("echo did not observe the delay")
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Millisecond)
	defer cancel()
	j3 := &queue.Job{
		ID:      "3",
		Type:    "echo",
		Payload: json.RawMessage(`{"delay":"5s"}`),
	}
	if err := r.Execute(ctx, j3); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("echo should respect ctx cancel, got %v", err)
	}
}

func TestFlakyFailsThenSucceedsByAttempt(t *testing.T) {
	r := NewRegistry()
	j := &queue.Job{
		ID:      "1",
		Type:    "flaky",
		Payload: json.RawMessage(`{"failTimes":2}`),
	}
	// Decision uses j.Attempts: attempts 1..2 fail, 3 succeeds.
	j.Attempts = 1
	if err := r.Execute(context.Background(), j); err == nil {
		t.Fatal("attempt 1 should fail")
	}
	j.Attempts = 2
	if err := r.Execute(context.Background(), j); err == nil {
		t.Fatal("attempt 2 should fail")
	}
	j.Attempts = 3
	if err := r.Execute(context.Background(), j); err != nil {
		t.Fatalf("attempt 3 should succeed, got %v", err)
	}
}

func TestFailAlwaysErrors(t *testing.T) {
	r := NewRegistry()
	j := &queue.Job{ID: "1", Type: "fail", Attempts: 1}
	if err := r.Execute(context.Background(), j); err == nil {
		t.Fatal("fail handler must return an error")
	}
}

func TestFuncAndCustomRegistration(t *testing.T) {
	r := &Registry{}
	called := false
	r.Register("custom", func(_ context.Context, _ *queue.Job) error {
		called = true
		return nil
	})
	if err := r.Execute(context.Background(), &queue.Job{Type: "custom"}); err != nil {
		t.Fatalf("custom: %v", err)
	}
	if !called {
		t.Fatal("custom handler not called")
	}

	fe := Func(func(context.Context, *queue.Job) error { return errors.New("x") })
	if err := fe.Execute(context.Background(), &queue.Job{}); err == nil {
		t.Fatal("Func adapter should invoke the wrapped function")
	}
}
