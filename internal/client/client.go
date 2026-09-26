// Package client is the fault-injecting HTTP client the business logic
// uses to call the fake payment gateway. It distinguishes three outcomes,
// which the idempotency layer treats very differently:
//
//   - definite success: gateway response received, money status known;
//   - definite failure: gateway responded with an error, nothing charged;
//   - AMBIGUOUS: timeout / connection reset / transport error. The charge
//     MAY have happened, and no local transaction can prove otherwise.
//
// The client supports two modes:
//
//   - Keyed mode sends Idempotency-Key on the charge. The gateway
//     deduplicates, so retrying an ambiguous call charges at most once
//     (the common real-world mitigation: end-to-end idempotency keys).
//   - Keyless mode does not. Retrying an ambiguous call can double-charge.
//     The tests include this case to show precisely what the local
//     idempotency layer does NOT guarantee.
package client

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"time"
)

// Outcome classifies a gateway call.
type Outcome string

const (
	// OutcomeSuccess: definite success, Charge is set.
	OutcomeSuccess Outcome = "success"
	// OutcomeFailed: definite failure, nothing charged.
	OutcomeFailed Outcome = "failed"
	// OutcomeAmbiguous: transport error/timeout; charge status unknown.
	OutcomeAmbiguous Outcome = "ambiguous"
)

// ChargeRequest is the business payment request.
type ChargeRequest struct {
	Amount    int    `json:"amount"`
	Currency  string `json:"currency"`
	Reference string `json:"reference,omitempty"`

	// IdempotencyKey, when non-empty, is forwarded to the gateway.
	IdempotencyKey string `json:"idempotency_key,omitempty"`

	// Fault optionally asks the fake gateway to inject one fault for this
	// key (see gateway.Fault* constants).
	Fault string `json:"-"`
}

// Charge mirrors the gateway's charge object.
type Charge struct {
	ID        string    `json:"id"`
	Key       string    `json:"key,omitempty"`
	Amount    int       `json:"amount"`
	Currency  string    `json:"currency"`
	Reference string    `json:"reference,omitempty"`
	CreatedAt time.Time `json:"created_at"`
}

// Result is what the caller gets.
type Result struct {
	Outcome    Outcome
	Charge     *Charge
	HTTPStatus int
	Attempt    int
	Err        error
}

// Attempt records one HTTP try for structured test output.
type Attempt struct {
	N          int    `json:"n"`
	Method     string `json:"method"`
	URL        string `json:"url"`
	HTTPStatus int    `json:"http_status,omitempty"`
	Outcome    string `json:"outcome"`
	Error      string `json:"error,omitempty"`
}

// Client calls the gateway. It does NOT itself retry ambiguous calls — the
// caller decides whether retrying is safe (keyed vs keyless).
type Client struct {
	BaseURL    string
	HTTPClient *http.Client
	// Trace, if set, receives one Attempt per HTTP call.
	Trace func(Attempt)
}

// New returns a Client with the given per-request timeout.
func New(baseURL string, timeout time.Duration) *Client {
	tr := &http.Transport{
		// Aggressive settings so injected resets surface quickly.
		MaxIdleConns:        10,
		IdleConnTimeout:     5 * time.Second,
		TLSHandshakeTimeout: timeout,
	}
	return &Client{
		BaseURL: baseURL,
		HTTPClient: &http.Client{
			Transport: tr,
			Timeout:   timeout,
		},
	}
}

type chargeResponse struct {
	Charge  Charge `json:"charge"`
	Deduped bool   `json:"deduped"`
}

// Charge performs exactly one HTTP attempt and classifies the outcome.
func (c *Client) Charge(ctx context.Context, n int, req ChargeRequest) Result {
	url := c.BaseURL + "/gateway/charge"
	body, _ := json.Marshal(req)
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return Result{Outcome: OutcomeAmbiguous, Attempt: n, Err: err}
	}
	httpReq.Header.Set("Content-Type", "application/json")
	if req.IdempotencyKey != "" {
		httpReq.Header.Set("Idempotency-Key", req.IdempotencyKey)
	}
	if req.Fault != "" {
		httpReq.Header.Set("X-Fault", req.Fault)
	}

	resp, err := c.HTTPClient.Do(httpReq)
	if err != nil {
		c.trace(Attempt{N: n, Method: "POST", URL: url, Outcome: string(OutcomeAmbiguous), Error: compactErr(err)})
		// Timeout/reset: the gateway may or may not have charged.
		return Result{Outcome: OutcomeAmbiguous, Attempt: n, Err: err}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))

	if resp.StatusCode >= 500 || resp.StatusCode == http.StatusTooManyRequests {
		c.trace(Attempt{N: n, Method: "POST", URL: url, HTTPStatus: resp.StatusCode, Outcome: string(OutcomeFailed), Error: string(raw)})
		return Result{Outcome: OutcomeFailed, HTTPStatus: resp.StatusCode, Attempt: n,
			Err: fmt.Errorf("gateway returned %d: %s", resp.StatusCode, bytes.TrimSpace(raw))}
	}
	if resp.StatusCode >= 400 {
		c.trace(Attempt{N: n, Method: "POST", URL: url, HTTPStatus: resp.StatusCode, Outcome: string(OutcomeFailed), Error: string(raw)})
		return Result{Outcome: OutcomeFailed, HTTPStatus: resp.StatusCode, Attempt: n,
			Err: fmt.Errorf("gateway rejected request (%d): %s", resp.StatusCode, bytes.TrimSpace(raw))}
	}

	var cr chargeResponse
	if err := json.Unmarshal(raw, &cr); err != nil {
		// We got a 2xx but cannot parse it: treat as ambiguous.
		c.trace(Attempt{N: n, Method: "POST", URL: url, HTTPStatus: resp.StatusCode, Outcome: string(OutcomeAmbiguous), Error: err.Error()})
		return Result{Outcome: OutcomeAmbiguous, HTTPStatus: resp.StatusCode, Attempt: n, Err: err}
	}
	c.trace(Attempt{N: n, Method: "POST", URL: url, HTTPStatus: resp.StatusCode, Outcome: string(OutcomeSuccess)})
	ch := cr.Charge
	return Result{Outcome: OutcomeSuccess, Charge: &ch, HTTPStatus: resp.StatusCode, Attempt: n}
}

// Metrics fetches gateway metrics (used by tests).
func (c *Client) Metrics(ctx context.Context) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.BaseURL+"/gateway/metrics", nil)
	if err != nil {
		return nil, err
	}
	resp, err := c.HTTPClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, errors.New("metrics: unexpected status " + resp.Status)
	}
	return io.ReadAll(io.LimitReader(resp.Body, 1<<20))
}

// Reset clears the fake gateway (used by tests).
func (c *Client) Reset(ctx context.Context) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+"/gateway/reset", nil)
	if err != nil {
		return err
	}
	resp, err := c.HTTPClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNoContent {
		return errors.New("reset: unexpected status " + resp.Status)
	}
	return nil
}

func (c *Client) trace(a Attempt) {
	if c.Trace != nil {
		c.Trace(a)
	}
}

func compactErr(err error) string {
	s := err.Error()
	if len(s) > 200 {
		s = s[:200] + "..."
	}
	return s
}
