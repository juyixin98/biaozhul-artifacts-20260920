// Package signclient is a small HMAC-signing HTTP client used by tests, the
// acceptance program and the README examples. It performs real request signing.
package signclient

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/example/rollout/internal/crypto"
)

type Client struct {
	BaseURL string
	KeyID   string
	Secret  []byte
	HTTP    *http.Client
}

func New(baseURL, keyID, secret string) *Client {
	return &Client{
		BaseURL: stringsTrimSlash(baseURL),
		KeyID:   keyID,
		Secret:  []byte(secret),
		HTTP:    &http.Client{Timeout: 15 * time.Second},
	}
}

// Do signs and executes one request. idempotencyKey may be empty.
func (c *Client) Do(ctx context.Context, method, path string, body []byte, idempotencyKey string) (*http.Response, []byte, error) {
	if body == nil {
		body = []byte{}
	}
	ts := time.Now().Unix()
	header, err := crypto.Sign(c.KeyID, c.Secret, method, path, ts, body)
	if err != nil {
		return nil, nil, fmt.Errorf("sign request: %w", err)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.BaseURL+path, bytes.NewReader(body))
	if err != nil {
		return nil, nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set(crypto.HMACHeader, header)
	if idempotencyKey != "" {
		req.Header.Set(crypto.IdempotencyHeader, idempotencyKey)
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, nil, err
	}
	defer resp.Body.Close()
	out, err := io.ReadAll(resp.Body)
	if err != nil {
		return resp, nil, err
	}
	return resp, out, nil
}

// Get issues an unsigned GET (reads are unauthenticated).
func (c *Client) Get(ctx context.Context, path string) (*http.Response, []byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.BaseURL+path, nil)
	if err != nil {
		return nil, nil, err
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, nil, err
	}
	defer resp.Body.Close()
	out, err := io.ReadAll(resp.Body)
	if err != nil {
		return resp, nil, err
	}
	return resp, out, err
}

// SignForCurl prints the header value for a shell invocation — handy when
// reproducing requests with curl.
func SignForCurl(keyID, secret, method, path string, body []byte) (string, error) {
	return crypto.Sign(keyID, []byte(secret), method, path, time.Now().Unix(), body)
}

// HeaderName is exported for examples.
func HeaderName() string { return crypto.HMACHeader }

func stringsTrimSlash(s string) string {
	for len(s) > 0 && s[len(s)-1] == '/' {
		s = s[:len(s)-1]
	}
	if s == "" {
		return fmt.Sprintf("http://localhost")
	}
	return s
}
