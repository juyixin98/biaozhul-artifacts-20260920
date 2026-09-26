// Package appclient is the fault-injecting client: it mediates between the
// circuit breaker and the in-process fake upstream, translating call results
// into breaker outcomes. A context cancel/deadline is reported as
// OutcomeCanceled, never as a service failure.
package appclient

import (
	"context"
	"errors"
	"sync"
	"time"

	"breakerhalfopen/internal/breaker"
	"breakerhalfopen/internal/clock"
	"breakerhalfopen/internal/upstream"
)

// Result classifies a client call for structured reports.
type Result string

const (
	ResultSuccess  Result = "success"
	ResultFailure  Result = "failure"  // upstream answered with a failure
	ResultRejected Result = "rejected" // breaker refused to start the call
	ResultCanceled Result = "canceled" // caller canceled / deadline elapsed
)

// Attempt is the structured record of one client call.
type Attempt struct {
	Seq        uint64        `json:"seq"`
	At         time.Time     `json:"at"`
	Result     Result        `json:"result"`
	Generation uint64        `json:"generation"`
	Probe      bool          `json:"probe"`
	Elapsed    time.Duration `json:"elapsed"`
	Err        string        `json:"err,omitempty"`
}

// Client wires breaker to fake upstream.
type Client struct {
	brk *breaker.Breaker
	up  *upstream.Upstream
	clk clock.Clock

	timeout time.Duration

	mu      sync.Mutex
	seq     uint64
	history []Attempt
}

// New builds a client. callTimeout <= 0 means no per-call deadline.
func New(brk *breaker.Breaker, up *upstream.Upstream, clk clock.Clock, callTimeout time.Duration) *Client {
	return &Client{
		brk:     brk,
		up:      up,
		clk:     clk,
		timeout: callTimeout,
	}
}

// Call runs one guarded call. parent may carry a deadline/cancel; when the
// client was configured with a callTimeout a clock-driven deadline is layered
// on top so virtual-time tests can force timeouts deterministically.
func (c *Client) Call(parent context.Context) Attempt {
	permit, err := c.brk.Allow()
	now := c.clk.Now()
	if errors.Is(err, breaker.ErrOpen) {
		return c.record(Attempt{At: now, Result: ResultRejected, Err: err.Error()})
	}
	if err != nil {
		return c.record(Attempt{At: now, Result: ResultFailure, Err: err.Error()})
	}

	ctx, cancel := context.WithCancel(parent)
	if c.timeout > 0 {
		ctx, cancel = clock.WithTimeout(parent, c.clk, c.timeout)
	}
	defer cancel()

	start := c.clk.Now()
	callErr := c.up.Call(ctx)
	elapsed := c.clk.Now().Sub(start)

	var res Result
	var outcome breaker.Outcome
	switch {
	case errors.Is(callErr, context.Canceled), errors.Is(callErr, context.DeadlineExceeded):
		res, outcome = ResultCanceled, breaker.OutcomeCanceled
	case errors.Is(callErr, upstream.ErrUpstreamFailure):
		res, outcome = ResultFailure, breaker.OutcomeFailure
	case callErr == nil:
		res, outcome = ResultSuccess, breaker.OutcomeSuccess
	default:
		res, outcome = ResultFailure, breaker.OutcomeFailure
	}
	permit.Done(outcome)

	return c.record(Attempt{
		At:         start,
		Result:     res,
		Generation: permitGeneration(permit),
		Probe:      permitIsProbe(permit),
		Elapsed:    elapsed,
		Err:        errString(callErr),
	})
}

// Since the breaker owns Permit fields, expose minimal introspection helpers
// by storing the flags back through a thin accessor.
func permitGeneration(p *breaker.Permit) uint64 { return p.Generation() }
func permitIsProbe(p *breaker.Permit) bool      { return p.IsProbe() }

func errString(e error) string {
	if e == nil {
		return ""
	}
	return e.Error()
}

func (c *Client) record(a Attempt) Attempt {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.seq++
	a.Seq = c.seq
	c.history = append(c.history, a)
	return a
}

// History returns a copy of all attempts so far.
func (c *Client) History() []Attempt {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]Attempt, len(c.history))
	copy(out, c.history)
	return out
}
