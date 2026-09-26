// Package faultclient is an HTTP client for the fake downstream service with
// controllable fault injection: forced transport errors, clock-driven
// timeouts, pre-call delays, and forced server-side failures. All timing
// goes through the clock.Clock abstraction so tests can drive it with a
// FakeClock.
package faultclient

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"sync"
	"time"

	"cancelprop/internal/clock"
)

// Faults describes what to inject on the next call.
type Faults struct {
	// FailRequest makes the client return a synthetic error without
	// contacting the downstream service.
	FailRequest bool
	// ForceDownstreamFail sends X-Fail: 1 so the fake service answers 500.
	ForceDownstreamFail bool
	// DelayBefore sleeps via the injected clock before issuing the request.
	DelayBefore time.Duration
	// Timeout bounds the whole call using the injected clock.
	Timeout time.Duration
}

// Result is the outcome of a successful (transport-wise) call.
type Result struct {
	StatusCode int
	Body       string
}

// Client is a fault-injecting HTTP client.
type Client struct {
	Name    string
	BaseURL string
	Clock   clock.Clock
	HTTP    *http.Client

	faultsMu sync.Mutex
	faults   Faults
}

// ErrInjected is returned when Faults.FailRequest is set.
var ErrInjected = errors.New("faultclient: injected request failure")

// New builds a client with an idle-connection-bounded transport.
func New(name, baseURL string, clk clock.Clock) *Client {
	tr := http.DefaultTransport.(*http.Transport).Clone()
	tr.MaxIdleConns = 16
	tr.MaxIdleConnsPerHost = 4
	tr.IdleConnTimeout = 30 * time.Second
	return &Client{
		Name:    name,
		BaseURL: baseURL,
		Clock:   clk,
		HTTP:    &http.Client{Transport: tr},
	}
}

// SetFaults atomically replaces the injected faults.
func (c *Client) SetFaults(f Faults) {
	c.faultsMu.Lock()
	c.faults = f
	c.faultsMu.Unlock()
}

// SnapshotFaults returns the current fault configuration.
func (c *Client) SnapshotFaults() Faults {
	c.faultsMu.Lock()
	defer c.faultsMu.Unlock()
	return c.faults
}

// CloseIdleConns releases pooled keep-alive connections.
func (c *Client) CloseIdleConns() { c.HTTP.CloseIdleConnections() }

// Call performs GET <BaseURL><path> with the currently configured faults.
// A 5xx response is returned as an error so callers can treat downstream
// failures uniformly; 4xx and other statuses return a Result.
func (c *Client) Call(ctx context.Context, path string) (*Result, error) {
	return c.CallWith(ctx, path, c.SnapshotFaults())
}

// CallWith is Call with explicit per-call faults, safe for concurrent use by
// many leaves sharing one client (no global fault state).
func (c *Client) CallWith(ctx context.Context, path string, f Faults) (*Result, error) {
	// A pre-call delay elapses before anything else, even when the call is
	// subsequently failed by injection; canceling during the delay aborts it.
	if f.DelayBefore > 0 {
		if err := c.Clock.Sleep(ctx, f.DelayBefore); err != nil {
			return nil, fmt.Errorf("%s: delayed call canceled: %w", c.Name, err)
		}
	}

	if f.FailRequest {
		return nil, fmt.Errorf("%s: %w", c.Name, ErrInjected)
	}

	callCtx := ctx
	if f.Timeout > 0 {
		var cancel context.CancelFunc
		callCtx, cancel = withClockTimeout(ctx, c.Clock, f.Timeout)
		defer cancel()
	}

	req, err := http.NewRequestWithContext(callCtx, http.MethodGet, c.BaseURL+path, nil)
	if err != nil {
		return nil, fmt.Errorf("%s: build request: %w", c.Name, err)
	}
	if f.ForceDownstreamFail {
		req.Header.Set("X-Fail", "1")
	}

	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, fmt.Errorf("%s: http call: %w", c.Name, err)
	}
	defer func() { _ = resp.Body.Close() }()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("%s: read body: %w", c.Name, err)
	}
	res := &Result{StatusCode: resp.StatusCode, Body: string(body)}
	if resp.StatusCode >= 500 {
		return res, fmt.Errorf("%s: downstream status %d", c.Name, resp.StatusCode)
	}
	return res, nil
}
