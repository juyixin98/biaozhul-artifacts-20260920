// Package client is a concurrent, digest-verifying client for the local
// cache HTTP API. Downloads are checked against the announced
// Content-Length and the SHA-256 digest; truncated or corrupt transfers are
// retried and, if they keep failing, reported as distinct errors.
package client

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"

	"localcache/api"
	"localcache/internal/cas"
)

var (
	// ErrTruncated marks a download that delivered fewer bytes than the
	// announced Content-Length (or whose body read failed mid-stream).
	ErrTruncated = errors.New("client: truncated download")
	// ErrChecksum marks a download whose content does not match the
	// requested digest.
	ErrChecksum = errors.New("client: digest mismatch on download")
)

// Client talks to one cache server.
type Client struct {
	Base    string
	HC      *http.Client
	Retries int // extra attempts after a truncated/corrupt download
}

// New returns a Client for a server base URL such as
// "http://127.0.0.1:8080".
func New(base string) *Client {
	return &Client{
		Base:    strings.TrimSuffix(base, "/"),
		HC:      &http.Client{},
		Retries: 2,
	}
}

// PutBytes uploads data and returns its digest. The digest is computed
// locally and the server's acknowledgement is checked against it.
func (c *Client) PutBytes(ctx context.Context, data []byte) (string, error) {
	digest := cas.DigestOf(data)
	req, err := http.NewRequestWithContext(ctx, http.MethodPut,
		c.Base+"/v1/cas/"+digest, bytes.NewReader(data))
	if err != nil {
		return "", err
	}
	req.ContentLength = int64(len(data))
	req.Header.Set("Content-Type", "application/octet-stream")
	resp, err := c.HC.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusCreated && resp.StatusCode != http.StatusOK {
		return "", decodeError(resp)
	}
	var pr api.PutResponse
	if err := json.NewDecoder(resp.Body).Decode(&pr); err != nil {
		return "", fmt.Errorf("decoding put response: %w", err)
	}
	if pr.Digest != digest {
		return "", fmt.Errorf("server acknowledged digest %q, want %q", pr.Digest, digest)
	}
	return digest, nil
}

// Has reports whether the server holds a complete object.
func (c *Client) Has(ctx context.Context, digest string) (bool, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodHead, c.Base+"/v1/cas/"+digest, nil)
	if err != nil {
		return false, err
	}
	resp, err := c.HC.Do(req)
	if err != nil {
		return false, err
	}
	resp.Body.Close()
	switch resp.StatusCode {
	case http.StatusOK:
		return true, nil
	case http.StatusNotFound:
		return false, nil
	default:
		return false, fmt.Errorf("unexpected HEAD status %s", resp.Status)
	}
}

// Get downloads an object and verifies it against its digest. Truncated or
// corrupt transfers are retried up to c.Retries times; other errors (e.g.
// not found) are returned immediately.
func (c *Client) Get(ctx context.Context, digest string) ([]byte, error) {
	var lastErr error
	for attempt := 0; attempt <= c.Retries; attempt++ {
		data, err := c.getOnce(ctx, digest)
		if err == nil {
			return data, nil
		}
		lastErr = err
		if !errors.Is(err, ErrTruncated) && !errors.Is(err, ErrChecksum) {
			return nil, err
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(time.Duration(attempt+1) * 50 * time.Millisecond):
		}
	}
	return nil, lastErr
}

func (c *Client) getOnce(ctx context.Context, digest string) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.Base+"/v1/cas/"+digest, nil)
	if err != nil {
		return nil, err
	}
	resp, err := c.HC.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, decodeError(resp)
	}
	var buf bytes.Buffer
	n, err := io.Copy(&buf, resp.Body)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrTruncated, err)
	}
	if resp.ContentLength >= 0 && n != resp.ContentLength {
		return nil, fmt.Errorf("%w: got %d of %d bytes", ErrTruncated, n, resp.ContentLength)
	}
	if got := cas.DigestOf(buf.Bytes()); got != digest {
		return nil, fmt.Errorf("%w: got %s, want %s", ErrChecksum, got, digest)
	}
	return buf.Bytes(), nil
}

// Build submits a fixture-command build request.
func (c *Client) Build(ctx context.Context, br *api.BuildRequest) (*api.BuildResult, error) {
	body, err := json.Marshal(br)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.Base+"/v1/builds", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.HC.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, decodeError(resp)
	}
	var res api.BuildResult
	if err := json.NewDecoder(resp.Body).Decode(&res); err != nil {
		return nil, fmt.Errorf("decoding build response: %w", err)
	}
	return &res, nil
}

// Stats fetches cache occupancy statistics.
func (c *Client) Stats(ctx context.Context) (*api.StatsResponse, error) {
	var out api.StatsResponse
	if err := c.getJSON(ctx, "/v1/admin/stats", &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// Fsck fetches a cache consistency report.
func (c *Client) Fsck(ctx context.Context) (*api.FsckReport, error) {
	var out api.FsckReport
	if err := c.getJSON(ctx, "/v1/admin/fsck", &out); err != nil {
		return nil, err
	}
	return &out, nil
}

func (c *Client) getJSON(ctx context.Context, path string, out any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.Base+path, nil)
	if err != nil {
		return err
	}
	resp, err := c.HC.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return decodeError(resp)
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

// PutMany uploads blobs concurrently and returns their digests in order.
func (c *Client) PutMany(ctx context.Context, blobs [][]byte, workers int) ([]string, error) {
	digests := make([]string, len(blobs))
	err := c.pool(ctx, len(blobs), workers, func(ctx context.Context, i int) error {
		d, err := c.PutBytes(ctx, blobs[i])
		if err != nil {
			return err
		}
		digests[i] = d
		return nil
	})
	return digests, err
}

// GetMany downloads digests concurrently and returns the blobs in order,
// each verified against its digest.
func (c *Client) GetMany(ctx context.Context, digests []string, workers int) ([][]byte, error) {
	blobs := make([][]byte, len(digests))
	err := c.pool(ctx, len(digests), workers, func(ctx context.Context, i int) error {
		b, err := c.Get(ctx, digests[i])
		if err != nil {
			return err
		}
		blobs[i] = b
		return nil
	})
	return blobs, err
}

// pool runs job(i) for i in [0,n) with at most `workers` goroutines,
// cancelling remaining work on the first error.
func (c *Client) pool(ctx context.Context, n, workers int, job func(context.Context, int) error) error {
	if workers < 1 {
		workers = 1
	}
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	jobs := make(chan int)
	var wg sync.WaitGroup
	var once sync.Once
	var firstErr error
	fail := func(err error) {
		once.Do(func() {
			firstErr = err
			cancel()
		})
	}
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := range jobs {
				if err := job(ctx, i); err != nil {
					fail(err)
				}
			}
		}()
	}
	for i := 0; i < n; i++ {
		select {
		case <-ctx.Done():
			wg.Wait()
			return firstErr
		case jobs <- i:
		}
	}
	close(jobs)
	wg.Wait()
	return firstErr
}

func decodeError(resp *http.Response) error {
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	var er api.ErrorResponse
	if json.Unmarshal(body, &er) == nil && er.Error != "" {
		return fmt.Errorf("server: %s (%s)", er.Error, resp.Status)
	}
	return fmt.Errorf("server: %s", resp.Status)
}
