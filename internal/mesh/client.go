package mesh

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strconv"
	"time"

	"retrybudget/internal/propagation"
	"retrybudget/internal/retry"
)

// Verdict labels carried on the wire (X-Retry-Verdict).
const (
	VerdictOK              = "ok"
	VerdictRetryable       = "retryable"
	VerdictNonRetryable    = "non-retryable"
	VerdictLocalExhausted  = "local-exhausted"
	VerdictBudgetExhausted = "budget-exhausted"
	VerdictDeadline        = "deadline"
	VerdictCanceled        = "canceled"
)

const headerVerdict = "X-Retry-Verdict"

// httpDoer is the single-round-trip HTTP surface (http.Client in production,
// a fake in tests).
type httpDoer interface {
	Do(req *http.Request) (*http.Response, error)
}

// roundTrip performs exactly one HTTP call, injects the current budget view,
// and reconciles downstream-reported cumulative attempt usage. It maps the
// response (or transport error) to the retry package's classified errors so
// that only explicitly safe failures are retried by the caller's loop.
func roundTrip(ctx context.Context, doer httpDoer, method, url string, b *retry.Budget, reqID string) (*http.Response, []byte, error) {
	req, err := http.NewRequestWithContext(ctx, method, url, nil)
	if err != nil {
		return nil, nil, retry.NonRetryable("build-request", err)
	}
	propagation.Inject(req.Header, reqID, b.Deadline(), b.Max(), b.Used())

	resp, err := doer.Do(req)
	if err != nil {
		// Context cancellation/deadline must surface as-is; anything else
		// from the transport is not automatically replayable.
		if ctx.Err() != nil {
			return nil, nil, ctx.Err()
		}
		return nil, nil, retry.NonRetryable("transport", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)

	if used, err := strconv.Atoi(resp.Header.Get(propagation.HeaderUsed)); err == nil {
		b.AckUsed(used)
	}
	return resp, body, classifyResponse(resp, body)
}

// classifyResponse turns a downstream response into nil (success), a
// retryable error (only when the response is explicitly safe to replay), a
// local/budget terminal verdict, or a non-retryable error.
func classifyResponse(resp *http.Response, body []byte) error {
	verdict := resp.Header.Get(headerVerdict)

	switch verdict {
	case VerdictBudgetExhausted:
		return &retry.Exhausted{Op: "downstream", Cause: errors.New(string(body))}
	case VerdictDeadline:
		return &retry.DeadlineExceeded{Op: "downstream", Cause: errors.New(string(body))}
	case VerdictLocalExhausted:
		// Downstream's own cap was hit, but the shared root still allows
		// work: safe for this layer to launch a fresh attempt.
		return &retry.LocalExhausted{Op: "downstream", Cause: errors.New(string(body))}
	case VerdictNonRetryable:
		return retry.NonRetryable("downstream", errors.New(truncate(body)))
	case VerdictCanceled:
		return context.Canceled
	}

	switch {
	case resp.StatusCode >= 200 && resp.StatusCode < 300:
		return nil
	case resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode == http.StatusServiceUnavailable:
		if after, ok := parseRetryAfter(resp.Header.Get("Retry-After")); ok {
			return retry.RetryableAfter("downstream", errors.New(resp.Status+" retry-after"), after)
		}
		return retry.Retryable("downstream", errors.New(resp.Status))
	default:
		// Fail safe: 4xx/5xx that are not 429/503 are never auto-replayed.
		return retry.NonRetryable("downstream", errors.New(resp.Status+": "+truncate(body)))
	}
}

func truncate(b []byte) string {
	s := string(b)
	if len(s) > 200 {
		return s[:200] + "…"
	}
	return s
}

// parseRetryAfter supports delta-seconds (RFC 9110 #10.2.3) and HTTP-date.
func parseRetryAfter(v string) (time.Duration, bool) {
	if v == "" {
		return 0, false
	}
	if secs, err := strconv.Atoi(v); err == nil {
		return time.Duration(secs) * time.Second, true
	}
	if t, err := http.ParseTime(v); err == nil {
		d := time.Until(t)
		if d < 0 {
			d = 0
		}
		return d, true
	}
	return 0, false
}
