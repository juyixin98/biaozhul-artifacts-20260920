package dag

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestFlakyFailsAcrossActivations(t *testing.T) {
	fn := DefaultRegistry()["flaky"]
	// fail_times=2: lifetime attempts 1 and 2 fail, 3 succeeds — even if
	// attempts happen in different processes (TotalAttempt is what matters).
	for _, attempt := range []int{1, 2} {
		if _, err := fn(context.Background(), Input{
			Params:       map[string]interface{}{"fail_times": float64(2), "succeed_with": "done"},
			TotalAttempt: attempt,
		}); err == nil {
			t.Fatalf("attempt %d: expected failure", attempt)
		}
	}
	v, err := fn(context.Background(), Input{
		Params:       map[string]interface{}{"fail_times": float64(2), "succeed_with": "done"},
		TotalAttempt: 3,
	})
	if err != nil {
		t.Fatalf("attempt 3: expected success, got %v", err)
	}
	if v != "done" {
		t.Fatalf("got %v, want done", v)
	}
}

func TestAddMulConcat(t *testing.T) {
	reg := DefaultRegistry()
	ctx := context.Background()
	in := Input{
		Params: map[string]interface{}{
			"values": []interface{}{float64(1), float64(2)},
			"sep":    "-",
		},
		Upstream: map[string]interface{}{"x": float64(3), "y": float64(4)},
	}
	if v, _ := reg["add"](ctx, in); v != int64(10) {
		t.Fatalf("add = %v (%T), want 10 int64", v, v)
	}
	if v, _ := reg["mul"](ctx, in); v != int64(24) {
		t.Fatalf("mul = %v, want 24", v)
	}
	if v, _ := reg["concat"](ctx, in); v != "1-2-3-4" {
		t.Fatalf("concat = %v, want 1-2-3-4", v)
	}
}

func TestCollectAndIdentity(t *testing.T) {
	reg := DefaultRegistry()
	in := Input{Params: map[string]interface{}{"value": "hi"}, Upstream: map[string]interface{}{"a": int64(1)}}
	v, err := reg["identity"](context.Background(), in)
	if err != nil || v != "hi" {
		t.Fatalf("identity = %v, %v", v, err)
	}
	cv, err := reg["collect"](context.Background(), in)
	if err != nil {
		t.Fatal(err)
	}
	m := cv.(map[string]interface{})
	if m["upstream"].(map[string]interface{})["a"] != int64(1) {
		t.Fatalf("collect upstream mismatch: %#v", m)
	}
}

func TestFail(t *testing.T) {
	if _, err := DefaultRegistry()["fail"](context.Background(),
		Input{Params: map[string]interface{}{"message": "boom"}, TotalAttempt: 1}); err == nil {
		t.Fatal("expected error")
	}
}

func TestSleepHonoursContext(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(20 * time.Millisecond)
		cancel()
	}()
	start := time.Now()
	_, err := DefaultRegistry()["sleep"](ctx, Input{Params: map[string]interface{}{"ms": float64(60000)}})
	if err == nil {
		t.Fatal("expected context cancellation error")
	}
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("want context.Canceled, got %v", err)
	}
	if time.Since(start) > time.Second {
		t.Fatal("sleep did not return promptly after cancel")
	}
}

func TestAddNonNumeric(t *testing.T) {
	_, err := DefaultRegistry()["add"](context.Background(), Input{
		Upstream: map[string]interface{}{"x": "not-a-number"},
	})
	if err == nil {
		t.Fatal("expected non-numeric error")
	}
}
