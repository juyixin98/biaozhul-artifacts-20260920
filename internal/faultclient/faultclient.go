// Package faultclient calls the fake upstream through a fault-injecting client
// and classifies the result for the circuit breaker.
//
// Injected faults:
//
//   - Virtual-time request timeout. The deadline is measured with the virtual
//     clock, so "a 100ms timeout" costs zero real time in tests.
//   - Deterministic transport errors: FailNextN calls and/or every Nth call.
//
// Outcome classification is the key contract here:
//
//   - a call abandoned because its PARENT context was canceled is
//     OutcomeCanceled and must never be recorded as a service failure;
//   - a timeout (DeadlineExceeded) is a service failure;
//   - 5xx responses and transport errors are service failures;
//   - 2xx/3xx/4xx responses are successes (the dependency answered).
package faultclient

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"sync"
	"sync/atomic"
	"time"

	"cbhalfopen/internal/breaker"
	"cbhalfopen/internal/fakeupstream"
	"cbhalfopen/internal/vclock"
)

// Config configures fault injection.
type Config struct {
	// Timeout is the per-call virtual-time deadline. Zero disables it.
	Timeout time.Duration `json:"timeout"`
	// FailNextN injects a transport error on the next N calls.
	FailNextN int `json:"fail_next_n"`
	// FailEveryNth injects a transport error every Nth call (N>0).
	FailEveryNth int `json:"fail_every_nth"`
}

// Result is the structured outcome of one client call.
type Result struct {
	Outcome     breaker.Outcome `json:"outcome"`
	StatusCode  int             `json:"status_code"`
	Reason      string          `json:"reason"`
	StartedAt   time.Time       `json:"started_at"`
	FinishedAt  time.Time       `json:"finished_at"`
	ElapsedVirt time.Duration   `json:"elapsed_virtual"`
	CallNumber  int64           `json:"call_number"`
	Generation  uint64          `json:"generation"`
	Probe       bool            `json:"probe"`
}

// Client is the fault-injecting caller.
type Client struct {
	fake *fakeupstream.Fake
	vc   *vclock.VirtualClock

	mu       sync.Mutex
	cfg      Config
	failLeft int

	seq atomic.Int64
}

// New creates a client.
func New(fake *fakeupstream.Fake, vc *vclock.VirtualClock, cfg Config) *Client {
	return &Client{fake: fake, vc: vc, cfg: cfg, failLeft: cfg.FailNextN}
}

// SetConfig atomically replaces the fault configuration. The FailNextN budget
// is reset to cfg.FailNextN.
func (c *Client) SetConfig(cfg Config) {
	c.mu.Lock()
	c.cfg = cfg
	c.failLeft = cfg.FailNextN
	c.mu.Unlock()
}

// Config returns a copy of the current configuration.
func (c *Client) Config() Config {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.cfg
}

// InjectFailures adds n transport-error injections to the current budget.
func (c *Client) InjectFailures(n int) {
	c.mu.Lock()
	c.failLeft += n
	c.mu.Unlock()
}

// CallOption tweaks a single Do call.
type CallOption func(*callSettings)

type callSettings struct {
	timeout    time.Duration
	timeoutSet bool
}

// WithTimeout overrides the configured timeout for one call. A zero duration
// disables the timeout for that call.
func WithTimeout(d time.Duration) CallOption {
	return func(s *callSettings) {
		s.timeout = d
		s.timeoutSet = true
	}
}

// Do performs one call under permit p. The permit is always completed exactly
// once: success, failure, or canceled according to the classification rules in
// the package doc.
//
// parent is the caller-controlled context: canceling it means the caller gave
// up on the call and is classified as canceled, never as a failure.
func (c *Client) Do(parent context.Context, p *breaker.Permit, opts ...CallOption) Result {
	n := c.seq.Add(1)
	start := c.vc.Now()

	res := Result{
		CallNumber: n,
		StartedAt:  start,
		Generation: p.Generation(),
		Probe:      p.IsProbe(),
	}

	c.mu.Lock()
	cfg := c.cfg
	injected := false
	if c.failLeft > 0 {
		c.failLeft--
		injected = true
	} else if cfg.FailEveryNth > 0 && n%int64(cfg.FailEveryNth) == 0 {
		injected = true
	}
	c.mu.Unlock()

	settings := callSettings{timeout: cfg.Timeout}
	for _, opt := range opts {
		opt(&settings)
	}

	ctx := parent
	var cancel context.CancelFunc
	if settings.timeout > 0 {
		ctx, cancel = vclock.ContextWithTimeout(parent, c.vc, settings.timeout)
	} else {
		ctx, cancel = context.WithCancel(parent)
	}
	defer cancel()

	if injected {
		// Client-side injected fault: no request reaches the upstream.
		res.StatusCode = 0
		res.Reason = "injected_transport_error"
		res.Outcome = breaker.OutcomeFailure
		p.RecordFailure()
		res.finish(c.vc.Now())
		return res
	}

	status, err := c.fake.Call(ctx)
	now := c.vc.Now()

	switch {
	case parent.Err() != nil && errors.Is(parent.Err(), context.Canceled):
		// Caller gave up. Even if the timeout also fired, caller
		// cancellation wins: this is not the dependency's fault.
		res.Outcome = breaker.OutcomeCanceled
		res.Reason = "caller_canceled"
		p.RecordCanceled()
	case err == nil && status < http.StatusInternalServerError:
		res.StatusCode = status
		res.Outcome = breaker.OutcomeSuccess
		res.Reason = fmt.Sprintf("http_%d", status)
		p.RecordSuccess()
	case err == nil:
		res.StatusCode = status
		res.Outcome = breaker.OutcomeFailure
		res.Reason = fmt.Sprintf("http_%d", status)
		p.RecordFailure()
	case errors.Is(err, context.DeadlineExceeded):
		res.Outcome = breaker.OutcomeFailure
		res.Reason = "timeout"
		p.RecordFailure()
	case errors.Is(err, context.Canceled):
		// Defensive: ctx canceled without parent cancellation (e.g.
		// timeout implementation detail). Treat as caller cancellation.
		res.Outcome = breaker.OutcomeCanceled
		res.Reason = "caller_canceled"
		p.RecordCanceled()
	default:
		res.Outcome = breaker.OutcomeFailure
		res.Reason = "transport_error: " + err.Error()
		p.RecordFailure()
	}

	res.finish(now)
	return res
}

func (r *Result) finish(end time.Time) {
	r.FinishedAt = end
	r.ElapsedVirt = end.Sub(r.StartedAt)
}
