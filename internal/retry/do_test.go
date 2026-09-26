package retry

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/example/retrybudget/internal/clock"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func stubResponse(status int, retryAfter string) *http.Response {
	h := http.Header{}
	if retryAfter != "" {
		h.Set("Retry-After", retryAfter)
	}
	return &http.Response{
		StatusCode: status,
		Header:     h,
		Body:       io.NopCloser(strings.NewReader("{}")),
	}
}

func makeRequest(ctx context.Context) func(int) (*http.Request, error) {
	return func(int) (*http.Request, error) {
		return http.NewRequestWithContext(ctx, http.MethodGet, "http://fake.local/x", nil)
	}
}

// autoAdvance keeps moving the fake clock forward until done closes, so
// retry sleeps complete without real waiting.
func autoAdvance(c *clock.Fake, done <-chan struct{}) {
	for {
		select {
		case <-done:
			return
		case <-time.After(time.Millisecond):
			c.Advance(100 * time.Millisecond)
		}
	}
}

func testCaller(c clock.Clock) Caller {
	return Caller{
		Clock:   c,
		Backoff: Backoff{Base: time.Second, Max: 10 * time.Second, Multiplier: 2},
	}
}

func TestDoRetriesThenSucceeds(t *testing.T) {
	fc := clock.NewFake(time.Unix(0, 0))
	calls := 0
	rt := roundTripFunc(func(*http.Request) (*http.Response, error) {
		calls++
		if calls < 3 {
			return stubResponse(http.StatusServiceUnavailable, ""), nil
		}
		return stubResponse(http.StatusOK, ""), nil
	})
	ctx := WithIdempotent(WithBudget(context.Background(), NewBudget(10)), true)
	done := make(chan struct{})
	go autoAdvance(fc, done)
	defer close(done)

	res := testCaller(fc).Do(ctx, makeRequest(ctx), rt)
	if res.Err != nil {
		t.Fatalf("err: %v", res.Err)
	}
	if res.Resp.StatusCode != 200 {
		t.Fatalf("status = %d", res.Resp.StatusCode)
	}
	if res.Attempts != 3 {
		t.Fatalf("attempts = %d, want 3", res.Attempts)
	}
	wantSleeps := []time.Duration{time.Second, 2 * time.Second}
	if len(res.Sleeps) != 2 || res.Sleeps[0] != wantSleeps[0] || res.Sleeps[1] != wantSleeps[1] {
		t.Fatalf("sleeps = %v, want %v", res.Sleeps, wantSleeps)
	}
}

func TestDoHonorsRetryAfter(t *testing.T) {
	fc := clock.NewFake(time.Unix(0, 0))
	calls := 0
	rt := roundTripFunc(func(*http.Request) (*http.Response, error) {
		calls++
		if calls == 1 {
			return stubResponse(http.StatusServiceUnavailable, "3"), nil
		}
		return stubResponse(http.StatusOK, ""), nil
	})
	ctx := WithIdempotent(WithBudget(context.Background(), NewBudget(5)), true)
	done := make(chan struct{})
	go autoAdvance(fc, done)
	defer close(done)

	res := testCaller(fc).Do(ctx, makeRequest(ctx), rt)
	if res.Err != nil {
		t.Fatalf("err: %v", res.Err)
	}
	if !res.UsedRetryAfter {
		t.Fatal("expected UsedRetryAfter")
	}
	if len(res.Sleeps) != 1 || res.Sleeps[0] != 3*time.Second {
		t.Fatalf("sleeps = %v, want [3s] (server-dictated, not backoff)", res.Sleeps)
	}
}

func TestDoBudgetExhausted(t *testing.T) {
	fc := clock.NewFake(time.Unix(0, 0))
	rt := roundTripFunc(func(*http.Request) (*http.Response, error) {
		return stubResponse(http.StatusServiceUnavailable, ""), nil
	})
	ctx := WithIdempotent(WithBudget(context.Background(), NewBudget(2)), true)
	done := make(chan struct{})
	go autoAdvance(fc, done)
	defer close(done)

	res := testCaller(fc).Do(ctx, makeRequest(ctx), rt)
	if res.Err != ErrBudgetExhausted {
		t.Fatalf("err = %v, want ErrBudgetExhausted", res.Err)
	}
	if res.Attempts != 2 {
		t.Fatalf("attempts = %d, want 2 (bounded by budget)", res.Attempts)
	}
}

func TestDoNonIdempotentNotRetried(t *testing.T) {
	fc := clock.NewFake(time.Unix(0, 0))
	rt := roundTripFunc(func(*http.Request) (*http.Response, error) {
		return stubResponse(http.StatusServiceUnavailable, ""), nil
	})
	ctx := WithIdempotent(WithBudget(context.Background(), NewBudget(10)), false)
	res := testCaller(fc).Do(ctx, makeRequest(ctx), rt)
	if res.Attempts != 1 {
		t.Fatalf("attempts = %d, want 1 (no retry for non-idempotent op)", res.Attempts)
	}
	if res.Resp == nil || res.Resp.StatusCode != 503 {
		t.Fatalf("expected final 503 response, got %+v", res.Resp)
	}
}

func TestDoCancellation(t *testing.T) {
	fc := clock.NewFake(time.Unix(0, 0))
	rt := roundTripFunc(func(*http.Request) (*http.Response, error) {
		return stubResponse(http.StatusServiceUnavailable, ""), nil
	})
	ctx, cancel := context.WithCancel(context.Background())
	ctx = WithIdempotent(WithBudget(ctx, NewBudget(10)), true)
	out := make(chan Result, 1)
	go func() { out <- testCaller(fc).Do(ctx, makeRequest(ctx), rt) }()
	time.Sleep(20 * time.Millisecond) // let the first attempt fail and the sleep begin
	cancel()
	res := <-out
	if res.Err != context.Canceled {
		t.Fatalf("err = %v, want context.Canceled", res.Err)
	}
	if res.Attempts != 1 {
		t.Fatalf("attempts = %d, want 1 (no attempt after cancel)", res.Attempts)
	}
}

// fakeDeadlineCtx carries a deadline expressed in fake-clock time; Done never
// fires, so only the caller's own deadline check (clk.Now vs Deadline) acts.
type fakeDeadlineCtx struct {
	context.Context
	dl time.Time
}

func (c fakeDeadlineCtx) Deadline() (time.Time, bool) { return c.dl, true }

func TestDoDeadlineStopsRetries(t *testing.T) {
	fc := clock.NewFake(time.Unix(0, 0))
	rt := roundTripFunc(func(*http.Request) (*http.Response, error) {
		return stubResponse(http.StatusServiceUnavailable, ""), nil
	})
	base := fakeDeadlineCtx{Context: context.Background(), dl: fc.Now().Add(1500 * time.Millisecond)}
	ctx := WithIdempotent(WithBudget(base, NewBudget(10)), true)
	done := make(chan struct{})
	go autoAdvance(fc, done)
	defer close(done)

	res := testCaller(fc).Do(ctx, makeRequest(ctx), rt)
	if res.Err != ErrDeadlineExceeded {
		t.Fatalf("err = %v, want ErrDeadlineExceeded", res.Err)
	}
	if res.Attempts != 2 {
		t.Fatalf("attempts = %d, want 2 (second retry would pass the deadline)", res.Attempts)
	}
}

func TestDoTransportErrorNonIdempotent(t *testing.T) {
	fc := clock.NewFake(time.Unix(0, 0))
	rt := roundTripFunc(func(*http.Request) (*http.Response, error) {
		return nil, io.ErrUnexpectedEOF
	})
	ctx := WithIdempotent(WithBudget(context.Background(), NewBudget(10)), false)
	res := testCaller(fc).Do(ctx, makeRequest(ctx), rt)
	if res.Err != io.ErrUnexpectedEOF {
		t.Fatalf("err = %v", res.Err)
	}
	if res.Attempts != 1 {
		t.Fatalf("attempts = %d, want 1", res.Attempts)
	}
}
