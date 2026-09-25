// Package client is the Go client for the model artifact cache.
//
// It is safe for concurrent use. Every download is verified against the
// claimed SHA-256 digest; DownloadToFile additionally resumes interrupted
// transfers with HTTP Range requests and publishes the result with an atomic
// rename, so a crashed download never leaves a seemingly-complete file.
package client

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"hash"
	"io"
	"math/rand"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"modelcache/digest"
	"modelcache/store"
)

// ErrDigestMismatch is returned when downloaded bytes do not hash to the
// requested digest.
var ErrDigestMismatch = errors.New("download digest mismatch")

// HTTPError carries a non-2xx API response.
type HTTPError struct {
	StatusCode int
	Code       string
	Message    string
}

func (e *HTTPError) Error() string {
	if e.Code != "" {
		return fmt.Sprintf("cache: unexpected HTTP %d: %s", e.StatusCode, e.Code)
	}
	return fmt.Sprintf("cache: unexpected HTTP %d: %s", e.StatusCode, e.Message)
}

// Options configures a Client.
type Options struct {
	BaseURL    string
	HTTPClient *http.Client // defaults to a client with no overall timeout
	MaxRetries int          // per-request attempts beyond the first; default 4
	UserAgent  string
}

// Client talks to a cacheapi server.
type Client struct {
	baseURL    string
	http       *http.Client
	maxRetries int
	userAgent  string
	jitterMu   sync.Mutex
	jitter     *rand.Rand
}

// New builds a Client.
func New(opts Options) *Client {
	hc := opts.HTTPClient
	if hc == nil {
		hc = &http.Client{
			// Individual calls should carry a context deadline; keep the
			// transport deadline-free so large artifacts are allowed.
			Timeout: 0,
		}
	}
	retries := opts.MaxRetries
	if retries <= 0 {
		retries = 4
	}
	return &Client{
		baseURL:    strings.TrimRight(opts.BaseURL, "/"),
		http:       hc,
		maxRetries: retries,
		userAgent:  opts.UserAgent,
		jitter:     rand.New(rand.NewSource(time.Now().UnixNano())),
	}
}

func (c *Client) url(path string) string { return c.baseURL + path }

func blobPath(d digest.Digest) string { return "/v1/blobs/" + d.String() }

// APIError parsing -----------------------------------------------------------

func decodeAPIError(status int, body []byte) *HTTPError {
	he := &HTTPError{StatusCode: status, Message: strings.TrimSpace(string(body))}
	var envelope struct {
		Error  string `json:"error"`
		Code   string `json:"code"`
		Detail string `json:"detail"`
	}
	if err := json.Unmarshal(body, &envelope); err == nil && envelope.Code != "" {
		he.Code = envelope.Code
		msg := envelope.Error
		if envelope.Detail != "" {
			msg = msg + ": " + envelope.Detail
		}
		he.Message = msg
	}
	return he
}

// retryableStatus reports transient response status codes.
func retryableStatus(code int) bool {
	switch code {
	case http.StatusRequestTimeout, http.StatusTooManyRequests,
		http.StatusBadGateway, http.StatusServiceUnavailable, http.StatusGatewayTimeout:
		return true
	}
	return false
}

func (c *Client) backoff(attempt int, resp *http.Response) time.Duration {
	if resp != nil {
		if ra := resp.Header.Get("Retry-After"); ra != "" {
			if secs, err := strconv.Atoi(ra); err == nil && secs >= 0 {
				return time.Duration(secs) * time.Second
			}
		}
	}
	base := 200 * time.Millisecond
	d := base << attempt // 200ms, 400ms, 800ms, ...
	if d > 2*time.Second {
		d = 2 * time.Second
	}
	c.jitterMu.Lock()
	j := time.Duration(c.jitter.Int63n(int64(base / 2)))
	c.jitterMu.Unlock()
	return d + j
}

func (c *Client) sleep(ctx context.Context, d time.Duration) error {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

func (c *Client) setCommon(req *http.Request) {
	if c.userAgent != "" {
		req.Header.Set("User-Agent", c.userAgent)
	}
}

// doJSON performs a request with no/JSON body and returns the response after
// retrying transport errors and transient statuses. Non-2xx responses are
// returned as *HTTPError with the body consumed.
func (c *Client) doJSON(ctx context.Context, method, path string, body []byte) ([]byte, http.Header, error) {
	var lastErr error
	for attempt := 0; attempt <= c.maxRetries; attempt++ {
		var rdr io.Reader
		if body != nil {
			rdr = bytes.NewReader(body)
		}
		req, err := http.NewRequestWithContext(ctx, method, c.url(path), rdr)
		if err != nil {
			return nil, nil, err
		}
		c.setCommon(req)
		if body != nil {
			req.Header.Set("Content-Type", "application/json")
		}
		resp, err := c.http.Do(req)
		if err != nil {
			lastErr = err
			if attempt < c.maxRetries {
				if serr := c.sleep(ctx, c.backoff(attempt, nil)); serr != nil {
					return nil, nil, serr
				}
				continue
			}
			return nil, nil, fmt.Errorf("cache: %s %s: %w", method, path, err)
		}
		respBody, readErr := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
		resp.Body.Close()
		if resp.StatusCode/100 == 2 {
			if readErr != nil {
				return nil, nil, readErr
			}
			return respBody, resp.Header, nil
		}
		he := decodeAPIError(resp.StatusCode, respBody)
		if retryableStatus(resp.StatusCode) && attempt < c.maxRetries {
			lastErr = he
			if serr := c.sleep(ctx, c.backoff(attempt, resp)); serr != nil {
				return nil, nil, serr
			}
			continue
		}
		return nil, nil, he
	}
	return nil, nil, lastErr
}

// Metadata / admin operations ------------------------------------------------

// Stat returns the size of a cached object via HEAD.
func (c *Client) Stat(ctx context.Context, dgst digest.Digest) (int64, error) {
	_, hdr, err := c.doJSON(ctx, http.MethodHead, blobPath(dgst), nil)
	if err != nil {
		return 0, err
	}
	if cl := hdr.Get("X-Content-Length"); cl != "" {
		return strconv.ParseInt(cl, 10, 64)
	}
	return 0, nil
}

// Exists reports whether an object is cached.
func (c *Client) Exists(ctx context.Context, dgst digest.Digest) (bool, error) {
	_, err := c.Stat(ctx, dgst)
	if err == nil {
		return true, nil
	}
	var he *HTTPError
	if errors.As(err, &he) && he.StatusCode == http.StatusNotFound {
		return false, nil
	}
	return false, err
}

// Stats returns cache statistics.
func (c *Client) Stats(ctx context.Context) (store.Stats, error) {
	body, _, err := c.doJSON(ctx, http.MethodGet, "/v1/stats", nil)
	if err != nil {
		return store.Stats{}, err
	}
	var st store.Stats
	if err := json.Unmarshal(body, &st); err != nil {
		return store.Stats{}, err
	}
	return st, nil
}

// List lists cached objects.
func (c *Client) List(ctx context.Context) ([]store.BlobInfo, error) {
	body, _, err := c.doJSON(ctx, http.MethodGet, "/v1/blobs", nil)
	if err != nil {
		return nil, err
	}
	var out struct {
		Blobs []store.BlobInfo `json:"blobs"`
	}
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, err
	}
	return out.Blobs, nil
}

// Delete evicts an object.
func (c *Client) Delete(ctx context.Context, dgst digest.Digest) error {
	_, _, err := c.doJSON(ctx, http.MethodDelete, blobPath(dgst), nil)
	return err
}

// Verify asks the server to re-hash an object, quarantining it if corrupt.
func (c *Client) Verify(ctx context.Context, dgst digest.Digest) (int64, error) {
	body, _, err := c.doJSON(ctx, http.MethodPost, blobPath(dgst)+"/verify", nil)
	if err != nil {
		return 0, err
	}
	var out struct {
		Size int64 `json:"size"`
	}
	if err := json.Unmarshal(body, &out); err != nil {
		return 0, err
	}
	return out.Size, nil
}

// Uploads --------------------------------------------------------------------

// PutResult describes a successful upload.
type PutResult struct {
	Digest  digest.Digest
	Size    int64
	Existed bool
}

// putSeeker uploads content that can be rewound (files, byte buffers), which
// lets the client retry a transport failure safely.
func (c *Client) putSeeker(ctx context.Context, dgst digest.Digest, r io.ReadSeeker) (PutResult, error) {
	var lastErr error
	for attempt := 0; attempt <= c.maxRetries; attempt++ {
		if attempt > 0 {
			if _, err := r.Seek(0, io.SeekStart); err != nil {
				return PutResult{}, err
			}
			if err := c.sleep(ctx, c.backoff(attempt, nil)); err != nil {
				return PutResult{}, err
			}
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodPut, c.url(blobPath(dgst)), r)
		if err != nil {
			return PutResult{}, err
		}
		c.setCommon(req)
		req.Header.Set("Content-Type", "application/octet-stream")
		resp, err := c.http.Do(req)
		if err != nil {
			lastErr = err
			if attempt < c.maxRetries {
				continue
			}
			return PutResult{}, fmt.Errorf("cache: upload failed: %w", err)
		}
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
		resp.Body.Close()
		if resp.StatusCode/100 == 2 {
			var pr PutResult
			var parsed struct {
				Digest  string `json:"digest"`
				Size    int64  `json:"size"`
				Existed bool   `json:"existed"`
			}
			if err := json.Unmarshal(body, &parsed); err != nil {
				return PutResult{}, err
			}
			pr.Digest = dgst
			pr.Size = parsed.Size
			pr.Existed = parsed.Existed
			return pr, nil
		}
		he := decodeAPIError(resp.StatusCode, body)
		// Only transient server responses are retried; a bad digest or an
		// over-size object will fail identically on the next attempt.
		if retryableStatus(resp.StatusCode) && attempt < c.maxRetries {
			lastErr = he
			if serr := c.sleep(ctx, c.backoff(attempt, resp)); serr != nil {
				return PutResult{}, serr
			}
			continue
		}
		return PutResult{}, he
	}
	return PutResult{}, fmt.Errorf("cache: upload failed after retries: %w", lastErr)
}

// PutBytes uploads an in-memory object.
func (c *Client) PutBytes(ctx context.Context, dgst digest.Digest, b []byte) (PutResult, error) {
	if want := digest.NewSHA256(b); want != dgst {
		return PutResult{}, fmt.Errorf("cache: local digest mismatch: want %s got %s", dgst, want)
	}
	return c.putSeeker(ctx, dgst, bytes.NewReader(b))
}

// PutFile hashes and uploads a local file.
func (c *Client) PutFile(ctx context.Context, path string) (PutResult, error) {
	f, err := os.Open(path)
	if err != nil {
		return PutResult{}, err
	}
	defer f.Close()
	dgst, size, err := digest.FromReader(f)
	if err != nil {
		return PutResult{}, err
	}
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		return PutResult{}, err
	}
	pr, err := c.putSeeker(ctx, dgst, f)
	if err != nil {
		return PutResult{}, err
	}
	pr.Digest = dgst
	if pr.Size == 0 && size > 0 {
		pr.Size = size
	}
	return pr, nil
}

// PutFilesConcurrently uploads several files with a worker pool. It returns
// per-file results; the first encountered error cancels the rest.
func (c *Client) PutFilesConcurrently(ctx context.Context, paths []string, workers int) ([]PutResult, error) {
	if workers <= 0 {
		workers = 4
	}
	results := make([]PutResult, len(paths))
	errs := make([]error, len(paths))
	jobs := make(chan int)
	var wg sync.WaitGroup
	if workers > len(paths) {
		workers = len(paths)
	}
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := range jobs {
				pr, err := c.PutFile(ctx, paths[i])
				results[i] = pr
				errs[i] = err
			}
		}()
	}
	for i := range paths {
		select {
		case jobs <- i:
		case <-ctx.Done():
			close(jobs)
			wg.Wait()
			return nil, ctx.Err()
		}
	}
	close(jobs)
	wg.Wait()
	for _, err := range errs {
		if err != nil {
			return results, err
		}
	}
	return results, nil
}

// Downloads ------------------------------------------------------------------

// verifiedReader wraps the response body with a running hash.
type verifiedReader struct {
	body       io.ReadCloser
	hasher     hash.Hash
	sum        string // expected hex
	bytesTotal int64
	got        int64
	checked    bool
}

func (v *verifiedReader) Read(p []byte) (int, error) {
	n, err := v.body.Read(p)
	if n > 0 {
		v.hasher.Write(p[:n])
		v.got += int64(n)
	}
	if err == io.EOF {
		if verr := v.check(); verr != nil {
			return n, verr
		}
	}
	return n, err
}

func (v *verifiedReader) check() error {
	if v.checked {
		return nil
	}
	v.checked = true
	if v.bytesTotal > 0 && v.got != v.bytesTotal {
		return io.ErrUnexpectedEOF
	}
	if hex.EncodeToString(v.hasher.Sum(nil)) != v.sum {
		return fmt.Errorf("%w: want %s got %s", ErrDigestMismatch, v.sum, hex.EncodeToString(v.hasher.Sum(nil)))
	}
	return nil
}

func (v *verifiedReader) Close() error { return v.body.Close() }

// Get streams a verified object. The returned reader must be closed; a digest
// mismatch surfaces as an error on Read (at or before io.EOF).
func (c *Client) Get(ctx context.Context, dgst digest.Digest) (io.ReadCloser, int64, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.url(blobPath(dgst)), nil)
	if err != nil {
		return nil, 0, err
	}
	c.setCommon(req)
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, 0, err
	}
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
		resp.Body.Close()
		return nil, 0, decodeAPIError(resp.StatusCode, body)
	}
	size := resp.ContentLength
	if xl := resp.Header.Get("X-Content-Length"); xl != "" {
		if n, perr := strconv.ParseInt(xl, 10, 64); perr == nil {
			size = n
		}
	}
	h := sha256.New()
	return &verifiedReader{body: resp.Body, hasher: h, sum: dgst.Hex(), bytesTotal: size}, size, nil
}

// DownloadToFile downloads dgst to dst with resumption and atomic publication.
//
// In-flight bytes live at dst+".part". If the transfer is interrupted the part
// is kept and the next attempt resumes with a Range request. A completed but
// bad-hash part is discarded once and re-fetched from scratch (the part itself
// may be corrupt). On success the part is fsynced and renamed onto dst.
func (c *Client) DownloadToFile(ctx context.Context, dgst digest.Digest, dst string) (int64, error) {
	partPath := dst + ".part"
	mismatchRestarts := 0
	const maxMismatchRestarts = 1

	var lastErr error
	for attempt := 0; attempt <= c.maxRetries; attempt++ {
		if attempt > 0 {
			if err := c.sleep(ctx, c.backoff(attempt, nil)); err != nil {
				return 0, err
			}
		}
		n, resumed, total, err := c.downloadAttempt(ctx, dgst, partPath)
		if err == nil {
			// Hash verified inside; publish atomically.
			if rerr := os.Rename(partPath, dst); rerr != nil {
				return 0, fmt.Errorf("cache: publish %s: %w", dst, rerr)
			}
			return n, nil
		}
		lastErr = err
		// A definitive client error (404, 416, ...) will not improve on retry.
		var he *HTTPError
		if errors.As(err, &he) && !retryableStatus(he.StatusCode) && he.StatusCode/100 == 4 {
			break
		}
		if errors.Is(err, ErrDigestMismatch) && mismatchRestarts < maxMismatchRestarts {
			// Full content arrived but failed verification (or the retained
			// part is rotten): discard and fetch the whole object again.
			mismatchRestarts++
			_ = os.Remove(partPath)
			continue
		}
		// Short/truncated reads keep the part for Range resume; other errors
		// are handled the same way since downloadAttempt revalidates the part.
		_ = resumed
		_ = total
	}
	return 0, fmt.Errorf("cache: download %s failed after retries: %w", dgst, lastErr)
}

// errRangeUnusable asks the download loop to discard its retained part and
// restart with a plain full GET on the next attempt.
var errRangeUnusable = errors.New("cache: unusable range response, restarting download")

// downloadAttempt performs one (possibly resumed) GET.
func (c *Client) downloadAttempt(ctx context.Context, dgst digest.Digest, partPath string) (n int64, resumed bool, total int64, err error) {
	part, err := os.OpenFile(partPath, os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return 0, false, 0, err
	}
	defer part.Close()

	offset, err := part.Seek(0, io.SeekEnd)
	if err != nil {
		return 0, false, 0, err
	}

	// Pre-seed the hasher with every retained byte.
	h := sha256.New()
	if offset > 0 {
		pf, oerr := os.Open(partPath)
		if oerr != nil {
			return 0, false, 0, oerr
		}
		pre, cerr := io.Copy(h, pf)
		pf.Close()
		if cerr != nil {
			return 0, false, 0, cerr
		}
		if pre != offset {
			return 0, false, 0, io.ErrUnexpectedEOF
		}
	}

	restartFull := func() (int64, bool, int64, error) {
		if rerr := part.Truncate(0); rerr != nil {
			return 0, offset > 0, 0, rerr
		}
		if _, serr := part.Seek(0, io.SeekStart); serr != nil {
			return 0, offset > 0, 0, serr
		}
		return 0, offset > 0, 0, errRangeUnusable
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.url(blobPath(dgst)), nil)
	if err != nil {
		return 0, false, 0, err
	}
	c.setCommon(req)
	if offset > 0 {
		req.Header.Set("Range", fmt.Sprintf("bytes=%d-", offset))
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return 0, offset > 0, 0, err
	}
	defer resp.Body.Close()

	switch {
	case resp.StatusCode == http.StatusOK && offset == 0:
		// Full body from scratch.
	case resp.StatusCode == http.StatusOK && offset > 0:
		// Server ignored the range: discard retained bytes and treat the new
		// stream as the full object. The outer loop will then GET from 0.
		return restartFull()
	case resp.StatusCode == http.StatusPartialContent && offset > 0:
		start, tot, perr := parseContentRange(resp.Header.Get("Content-Range"))
		if perr != nil || start != offset {
			return restartFull()
		}
		total = tot
	default:
		// 416 in reply to a resume means the retained part is longer than
		// the object; discard it and restart from offset 0.
		if resp.StatusCode == http.StatusRequestedRangeNotSatisfiable && offset > 0 {
			return restartFull()
		}
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
		return 0, offset > 0, 0, decodeAPIError(resp.StatusCode, body)
	}

	if total == 0 {
		if xl := resp.Header.Get("X-Content-Length"); xl != "" {
			total, _ = strconv.ParseInt(xl, 10, 64)
		}
	}

	mw := io.MultiWriter(part, h)
	n, copyErr := io.Copy(mw, resp.Body)
	got := offset + n

	// Flush retained bytes before hashing/renaming.
	if syncErr := part.Sync(); syncErr != nil {
		return got, offset > 0, total, syncErr
	}

	if copyErr != nil {
		// Broken connection mid-body: keep the part for resume.
		return got, offset > 0, total, copyErr
	}

	// Clean EOF. Check completeness against the declared size when known.
	if total > 0 && got != total {
		return got, offset > 0, total, io.ErrUnexpectedEOF
	}

	if hex.EncodeToString(h.Sum(nil)) != dgst.Hex() {
		return got, offset > 0, total,
			fmt.Errorf("%w: want %s", ErrDigestMismatch, dgst.Hex())
	}
	return got, offset > 0, total, nil
}

// parseContentRange parses "bytes start-end/total", e.g. "bytes 100-199/500".
func parseContentRange(v string) (start, total int64, err error) {
	const prefix = "bytes "
	if !strings.HasPrefix(v, prefix) {
		return 0, 0, fmt.Errorf("unexpected Content-Range %q", v)
	}
	spec := strings.TrimPrefix(v, prefix)
	rangePart, totalPart, ok := strings.Cut(spec, "/")
	if !ok {
		return 0, 0, fmt.Errorf("unexpected Content-Range %q", v)
	}
	startStr, _, ok := strings.Cut(rangePart, "-")
	if !ok {
		return 0, 0, fmt.Errorf("unexpected Content-Range %q", v)
	}
	start, err = strconv.ParseInt(startStr, 10, 64)
	if err != nil {
		return 0, 0, err
	}
	if totalPart == "*" {
		return start, 0, nil
	}
	total, err = strconv.ParseInt(totalPart, 10, 64)
	if err != nil {
		return 0, 0, err
	}
	return start, total, nil
}
