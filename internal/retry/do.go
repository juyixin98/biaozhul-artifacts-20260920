package retry

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strconv"
	"time"

	"github.com/example/retrybudget/internal/clock"
)

// Terminal outcomes of a call, surfaced in structured results.
var (
	// ErrBudgetExhausted means the shared retry budget ran out before an
	// attempt could be made; no request was sent for that attempt.
	ErrBudgetExhausted = errors.New("retry budget exhausted")
	// ErrDeadlineExceeded means the propagated end-to-end deadline leaves no
	// room for another attempt plus its backoff delay.
	ErrDeadlineExceeded = errors.New("end-to-end deadline exceeded")
)

// Result summarizes one logical call (initial try plus any retries).
type Result struct {
	Resp           *http.Response // final response, nil on transport error
	Attempts       int            // attempts actually sent
	Sleeps         []time.Duration
	UsedRetryAfter bool
	Err            error // nil on a final HTTP response (even 4xx/5xx)
}

// Caller executes HTTP calls with budget-aware retries.
type Caller struct {
	Clock   clock.Clock
	Backoff Backoff
}

// RoundTripper matches http.RoundTripper's core method.
type RoundTripper interface {
	RoundTrip(*http.Request) (*http.Response, error)
}

// Do runs makeReq/roundTrip until success, a non-retryable outcome, budget
// exhaustion, deadline, or cancellation. Each attempt consumes one unit of
// the budget found in ctx (if any). Retries happen only when the operation
// is marked idempotent in ctx and the failure is retryable.
func (c Caller) Do(ctx context.Context, makeReq func(attempt int) (*http.Request, error), roundTrip RoundTripper) Result {
	clk := c.Clock
	if clk == nil {
		clk = clock.Real{}
	}
	var res Result
	for attempt := 1; ; attempt++ {
		if err := ctx.Err(); err != nil {
			res.Err = err
			return res
		}
		if b := BudgetFrom(ctx); b != nil && !b.TryAcquire() {
			res.Err = ErrBudgetExhausted
			return res
		}
		req, err := makeReq(attempt)
		if err != nil {
			res.Err = err
			return res
		}
		resp, err := roundTrip.RoundTrip(req)
		res.Attempts++
		if err != nil {
			if ctx.Err() != nil {
				res.Err = ctx.Err()
				return res
			}
			if !IdempotentFrom(ctx) {
				res.Err = err
				return res
			}
			if !c.waitBeforeRetry(ctx, clk, c.Backoff.Delay(attempt), &res) {
				return res
			}
			continue
		}
		if !retryableStatus(resp.StatusCode) || !IdempotentFrom(ctx) {
			res.Resp = resp
			return res
		}
		delay, ok := parseRetryAfter(resp.Header.Get("Retry-After"), clk.Now())
		io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
		if !ok {
			delay = c.Backoff.Delay(attempt)
		} else {
			res.UsedRetryAfter = true
		}
		if !c.waitBeforeRetry(ctx, clk, delay, &res) {
			return res
		}
	}
}

// waitBeforeRetry sleeps for the given retry delay, returning false when the
// wait was cut short by the propagated deadline or by cancellation; res.Err
// is set accordingly.
func (c Caller) waitBeforeRetry(ctx context.Context, clk clock.Clock, delay time.Duration, res *Result) bool {
	if delay < 0 {
		delay = 0
	}
	if dl, ok := ctx.Deadline(); ok && !clk.Now().Add(delay).Before(dl) {
		res.Err = ErrDeadlineExceeded
		return false
	}
	res.Sleeps = append(res.Sleeps, delay)
	if err := clk.Sleep(ctx, delay); err != nil {
		res.Err = err
		return false
	}
	return true
}

// retryableStatus classifies statuses that may be retried when the operation
// is idempotent: 408, 429 and 5xx.
func retryableStatus(code int) bool {
	return code == http.StatusRequestTimeout ||
		code == http.StatusTooManyRequests ||
		(code >= 500 && code <= 599)
}

// parseRetryAfter supports delta-seconds and HTTP-date forms.
func parseRetryAfter(v string, now time.Time) (time.Duration, bool) {
	if v == "" {
		return 0, false
	}
	if secs, err := strconv.Atoi(v); err == nil {
		if secs < 0 {
			secs = 0
		}
		return time.Duration(secs) * time.Second, true
	}
	if t, err := http.ParseTime(v); err == nil {
		if d := time.Until(t); d > 0 {
			return d, true
		}
		return 0, true
	}
	return 0, false
}
