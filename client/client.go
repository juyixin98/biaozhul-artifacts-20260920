// Package client is a deliberately fault-injecting HTTP client for the
// conditional-update service.
//
// It wraps an *http.Client transport with FailurePolicy, which can delay
// requests and/or return synthetic errors deterministically. On top of that it
// offers UpdateWithRetry, a compare-and-swap (optimistic concurrency) loop:
// GET the current ETag, PUT with If-Match, and on 412 re-GET and retry. Every
// attempt is recorded as a structured Attempt so tests and the verification
// command can report exactly what happened.
package client

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// FailurePolicy configures transport-level fault injection.
type FailurePolicy struct {
	// FailFirst causes the first N outgoing requests to fail with a synthetic
	// error before they are sent.
	FailFirst int
	// Latency is added to every request.
	Latency time.Duration
	// sleepy advances or sleeps for Latency. Tests inject a fake sleeper so
	// injected latency costs no real time.
	sleepy func(time.Duration)
	calls  int
}

// NewFailurePolicy builds a policy. If sleeper is nil, time.Sleep is used.
func NewFailurePolicy(failFirst int, latency time.Duration, sleeper func(time.Duration)) *FailurePolicy {
	if sleeper == nil {
		sleeper = time.Sleep
	}
	return &FailurePolicy{FailFirst: failFirst, Latency: latency, sleepy: sleeper}
}

// Attempt is one structured request record.
type Attempt struct {
	Method     string        `json:"method"`
	Path       string        `json:"path"`
	IfMatch    string        `json:"ifMatch,omitempty"`
	Outcome    string        `json:"outcome"` // "ok", "http_error", "transport_error", "precondition_failed", "precondition_required", "weak_etag_rejected", "not_found"
	StatusCode int           `json:"statusCode,omitempty"`
	ETag       string        `json:"etag,omitempty"`
	Elapsed    time.Duration `json:"elapsed"`
	Error      string        `json:"error,omitempty"`
}

// Client is the fault-injecting HTTP client.
type Client struct {
	base   string
	httpc  *http.Client
	policy *FailurePolicy
}

func New(baseURL string, httpc *http.Client, policy *FailurePolicy) *Client {
	if httpc == nil {
		httpc = &http.Client{Timeout: 5 * time.Second}
	}
	if policy == nil {
		policy = NewFailurePolicy(0, 0, nil)
	}
	return &Client{base: strings.TrimRight(baseURL, "/"), httpc: httpc, policy: policy}
}

// Create POSTs a new resource and returns its strong ETag.
func (c *Client) Create(ctx context.Context, key string, body []byte) (*Result, error) {
	return c.do(ctx, http.MethodPost, "/resources/"+key, "", body)
}

// Get reads a resource.
func (c *Client) Get(ctx context.Context, key string) (*Result, error) {
	return c.do(ctx, http.MethodGet, "/resources/"+key, "", nil)
}

// Update PUTs with an explicit If-Match tag.
func (c *Client) Update(ctx context.Context, key, ifMatch string, body []byte) (*Result, error) {
	return c.do(ctx, http.MethodPut, "/resources/"+key, ifMatch, body)
}

// Delete removes a resource with a precondition.
func (c *Client) Delete(ctx context.Context, key, ifMatch string) (*Result, error) {
	return c.do(ctx, http.MethodDelete, "/resources/"+key, ifMatch, nil)
}

// Result is the structured outcome of one logical HTTP call.
type Result struct {
	Attempt
	Body []byte
}

func (c *Client) do(ctx context.Context, method, path, ifMatch string, body []byte) (*Result, error) {
	start := time.Now()
	rec := Attempt{Method: method, Path: path, IfMatch: ifMatch}

	if c.policy.Latency > 0 {
		c.policy.sleepy(c.policy.Latency)
	}
	if c.policy.FailFirst > 0 {
		c.policy.FailFirst--
		c.policy.calls++
		rec.Elapsed = time.Since(start)
		rec.Outcome = "transport_error"
		rec.Error = "synthetic transport failure (injected)"
		return &Result{Attempt: rec}, fmt.Errorf("%s", rec.Error)
	}

	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.base+path, reader)
	if err != nil {
		return nil, fmt.Errorf("build request: %w", err)
	}
	if ifMatch != "" {
		req.Header.Set("If-Match", ifMatch)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/octet-stream")
	}

	resp, err := c.httpc.Do(req)
	rec.Elapsed = time.Since(start)
	if err != nil {
		rec.Outcome = "transport_error"
		rec.Error = err.Error()
		return &Result{Attempt: rec}, err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))

	rec.StatusCode = resp.StatusCode
	rec.ETag = resp.Header.Get("ETag")
	rec.Outcome = classify(resp.StatusCode, data)
	return &Result{Attempt: rec, Body: data}, nil
}

func classify(status int, body []byte) string {
	switch status {
	case http.StatusOK, http.StatusCreated, http.StatusNoContent:
		return "ok"
	case http.StatusPreconditionFailed:
		var eb struct{ Error string }
		if json.Unmarshal(body, &eb) == nil && eb.Error == "weak_etag_rejected" {
			return "weak_etag_rejected"
		}
		return "precondition_failed"
	case http.StatusPreconditionRequired:
		return "precondition_required"
	case http.StatusNotFound:
		return "not_found"
	default:
		return "http_error"
	}
}

// CasReport is the structured output of an UpdateWithRetry run.
type CasReport struct {
	Key          string    `json:"key"`
	Attempts     []Attempt `json:"attempts"`
	Success      bool      `json:"success"`
	FinalETag    string    `json:"finalETag,omitempty"`
	FinalStatus  int       `json:"finalStatus,omitempty"`
	Retries      int       `json:"retries"`
	TransportErr string    `json:"transportError,omitempty"`
}

// UpdateWithRetry implements optimistic concurrency: fetch ETag, conditionally
// PUT, re-fetch on 412. It gives up after maxAttempts round-trips. The mutate
// function produces the desired body for the current content/version; returning
// an error aborts the loop.
func (c *Client) UpdateWithRetry(ctx context.Context, key string, maxAttempts int, mutate func(current []byte) ([]byte, error)) (*CasReport, error) {
	report := &CasReport{Key: key}
	if maxAttempts < 1 {
		maxAttempts = 1
	}
	for i := 0; i < maxAttempts; i++ {
		got, err := c.Get(ctx, key)
		report.Attempts = append(report.Attempts, got.Attempt)
		if err != nil {
			report.TransportErr = err.Error()
			return report, err
		}
		if got.StatusCode != http.StatusOK {
			report.FinalStatus = got.StatusCode
			return report, nil
		}

		newBody, merr := mutate(got.Body)
		if merr != nil {
			return report, merr
		}
		put, err := c.Update(ctx, key, got.ETag, newBody)
		report.Attempts = append(report.Attempts, put.Attempt)
		if err != nil {
			report.TransportErr = err.Error()
			return report, err
		}
		if put.StatusCode == http.StatusOK {
			report.Success = true
			report.FinalETag = put.ETag
			report.FinalStatus = put.StatusCode
			report.Retries = i
			return report, nil
		}
		if put.StatusCode != http.StatusPreconditionFailed {
			report.FinalStatus = put.StatusCode
			return report, nil
		}
		// 412: another writer won; loop and re-GET the new ETag.
	}
	report.FinalStatus = http.StatusPreconditionFailed
	return report, nil
}

// VersionFromTag extracts the version number embedded in a tag, for logging.
func VersionFromTag(tag string) (int64, error) {
	tag = strings.TrimPrefix(tag, "W/")
	tag = strings.Trim(tag, `"`)
	if !strings.HasPrefix(tag, "v") {
		return 0, fmt.Errorf("not a versioned tag: %q", tag)
	}
	rest := tag[1:]
	if i := strings.IndexByte(rest, '-'); i >= 0 {
		rest = rest[:i]
	}
	return strconv.ParseInt(rest, 10, 64)
}
