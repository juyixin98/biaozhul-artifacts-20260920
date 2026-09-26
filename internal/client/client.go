// Package client is the fault-injecting HTTP client used to verify the
// range service. It downloads artifacts via byte-range requests, retries
// transient failures through a controllable clock, reassembles multipart
// responses, and verifies the result against the artifact's original bytes
// (SHA-256) while emitting a structured report.
package client

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime"
	"mime/multipart"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/example/rangeserver/internal/artifact"
	"github.com/example/rangeserver/internal/clock"
	"github.com/example/rangeserver/internal/rangespec"
)

// MaxRetries is the total attempts per HTTP request (1 + retries).
const MaxRetries = 4

// defaultBackoff is advanced through the injected Clock so tests can use a
// FakeClock and never actually sleep.
var defaultBackoff = []time.Duration{50 * time.Millisecond, 200 * time.Millisecond, time.Second}

// Attempt records one HTTP request attempt for the structured report.
type Attempt struct {
	Number        int    `json:"number"`
	RangeHeader   string `json:"range_header,omitempty"`
	Status        int    `json:"status,omitempty"`
	Decision      string `json:"decision,omitempty"`
	BytesReceived int64  `json:"bytes_received"`
	RetryReason   string `json:"retry_reason,omitempty"`
	Error         string `json:"error,omitempty"`
	DurationMS    int64  `json:"duration_ms"`
}

// Segment is one reassembled byte interval.
type Segment struct {
	First       int64  `json:"first"`
	Last        int64  `json:"last"`
	Length      int64  `json:"length"`
	SHA256      string `json:"sha256"`
	Overlapping bool   `json:"overlapping_already_delivered_bytes"`
}

// Report is the structured result of one download.
type Report struct {
	ArtifactID                 string    `json:"artifact_id"`
	URL                        string    `json:"url"`
	StartedAt                  time.Time `json:"started_at"`
	TotalDurationMS            int64     `json:"total_duration_ms"`
	Encoding                   string    `json:"encoding"`
	RepresentationSize         int64     `json:"representation_size"`
	ExpectedSHA256             string    `json:"expected_sha256"`
	ActualSHA256               string    `json:"actual_sha256,omitempty"`
	RequestedRanges            []string  `json:"requested_ranges"`
	OverlappingRangesRequested bool      `json:"overlapping_ranges_requested"`
	Attempts                   []Attempt `json:"attempts"`
	Segments                   []Segment `json:"segments"`
	ReassembledBytes           int64     `json:"reassembled_bytes"`
	CompleteCoverage           bool      `json:"complete_coverage"`
	Verified                   bool      `json:"verified"`
	StatusSummary              string    `json:"status_summary"`
	FatalError                 string    `json:"fatal_error,omitempty"`
}

// Config configures a Client.
type Config struct {
	BaseURL   string
	HTTP      *http.Client
	Clock     clock.Clock
	MaxRanges int // mirrors the server limit so the client can fail fast
	// AcceptGzip makes Download request the gzip selected representation.
	// Range offsets are representation-relative, so gzip downloads must use
	// the whole representation (no Range header); the client gunzips after
	// the download and verifies the canonical bytes.
	AcceptGzip bool
}

// Client downloads and verifies artifacts.
type Client struct {
	baseURL    string
	http       *http.Client
	clk        clock.Clock
	maxRanges  int
	acceptGzip bool
}

// New constructs a Client.
func New(cfg Config) *Client {
	hc := cfg.HTTP
	if hc == nil {
		hc = http.DefaultClient
	}
	clk := cfg.Clock
	if clk == nil {
		clk = clock.RealClock{}
	}
	return &Client{
		baseURL:    strings.TrimRight(cfg.BaseURL, "/"),
		http:       hc,
		clk:        clk,
		maxRanges:  cfg.MaxRanges,
		acceptGzip: cfg.AcceptGzip,
	}
}

// Metadata is the subset of the listing entry the client needs.
type Metadata struct {
	ID     string `json:"id"`
	ETag   string `json:"etag"`
	Size   int    `json:"size"`
	SHA256 string `json:"sha256"`
}

// ListArtifacts fetches the catalog from the in-process fake service.
func (c *Client) ListArtifacts(ctx context.Context) ([]Metadata, error) {
	body, _, _, _, err := c.request(ctx, http.MethodGet, c.baseURL+"/artifacts", "", nil)
	if err != nil {
		return nil, fmt.Errorf("listing artifacts: %w", err)
	}
	var resp struct {
		Artifacts []Metadata `json:"artifacts"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		return nil, fmt.Errorf("decoding catalog: %w", err)
	}
	return resp.Artifacts, nil
}

// DownloadResult pairs the report with the reassembled canonical bytes.
type DownloadResult struct {
	Report  Report
	Payload []byte
}

// Download fetches id using the given raw byte-range-spec strings (for
// example "0-99", "-64", "500-"), attaching If-Range with the current
// validator. Requested ranges are expected to tile the whole
// representation: gaps, double-delivered bytes, and hash mismatches are all
// reported as failures.
func (c *Client) Download(ctx context.Context, id string, ranges []string) (*DownloadResult, error) {
	if c.maxRanges > 0 && len(ranges) > c.maxRanges {
		return nil, fmt.Errorf("client: %d ranges exceed client limit of %d", len(ranges), c.maxRanges)
	}
	if c.acceptGzip && len(ranges) > 0 {
		return nil, errors.New("client: gzip downloads cannot carry byte ranges (offsets are representation-relative)")
	}

	var requestedSpecs []rangespec.Spec
	if len(ranges) > 0 {
		specs, err := rangespec.ParseHeader("bytes=" + strings.Join(ranges, ", "))
		if err != nil {
			return nil, fmt.Errorf("client: %w", err)
		}
		requestedSpecs = specs
	}

	report := Report{
		ArtifactID:      id,
		URL:             c.baseURL + "/artifacts/" + id,
		StartedAt:       c.clk.Now(),
		RequestedRanges: append([]string(nil), ranges...),
	}
	start := c.clk.Now()
	defer func() { report.TotalDurationMS = c.clk.Now().Sub(start).Milliseconds() }()

	meta, err := c.lookupMetadata(ctx, id)
	if err != nil {
		return fail(&report, "metadata-lookup-failed", err), err
	}
	report.ExpectedSHA256 = meta.SHA256

	if c.acceptGzip {
		return c.downloadGzip(ctx, &report, meta)
	}

	rangeHeader := ""
	if len(ranges) > 0 {
		rangeHeader = "bytes=" + strings.Join(ranges, ", ")
	}
	body, hdr, status, attempts, err := c.request(ctx, http.MethodGet, report.URL, rangeHeader, map[string]string{
		"If-Range": meta.ETag,
	})
	report.Attempts = attempts
	if err != nil {
		return fail(&report, "download-failed", err), err
	}

	encoding := hdr.Get("Content-Encoding")
	report.Encoding = encodingOrIdentity(encoding)

	cov := newCoverage(int64(meta.Size))
	assembly := make([]byte, meta.Size)

	switch {
	case status == http.StatusPartialContent && strings.HasPrefix(hdr.Get("Content-Type"), "multipart/byteranges"):
		err = c.collectMultipart(hdr, body, &report, cov, assembly)
	case status == http.StatusPartialContent:
		err = c.collectSingle(hdr, body, &report, cov, assembly)
	case status == http.StatusOK:
		// Range ignored (malformed / stale If-Range): full representation.
		err = placeFull(body, &report, cov, assembly)
	case status == http.StatusRequestedRangeNotSatisfiable:
		err = errors.New("server returned 416 Requested Range Not Satisfiable")
	default:
		err = fmt.Errorf("unexpected status %d", status)
	}
	if err != nil {
		return fail(&report, "reassembly-failed", err), err
	}

	// Determine whether the *requested* set contained overlaps, independent
	// of the server's coalescing. Resolve needs the representation size:
	// gzip changes it, so resolve only for identity here (overlap is a
	// property of request construction either way).
	if len(requestedSpecs) > 1 {
		resolved, rerr := rangespec.ResolveAll(requestedSpecs, int64(meta.Size))
		report.OverlappingRangesRequested = rerr == nil && len(resolved) < countIntervals(requestedSpecs, int64(meta.Size))
	}

	report.RepresentationSize = cov.deliveredCount()
	report.ReassembledBytes = int64(len(assembly))
	if !cov.complete() {
		err := fmt.Errorf("incomplete coverage: missing %d of %d bytes", int64(meta.Size)-cov.deliveredCount(), meta.Size)
		return fail(&report, "incomplete-coverage", err), err
	}
	report.CompleteCoverage = true

	canonical := assembly
	if report.Encoding == artifact.RepGzip {
		canonical, err = artifact.Gunzip(assembly)
		if err != nil {
			return fail(&report, "decode-failed", fmt.Errorf("gunzip representation: %w", err)), err
		}
	}
	if len(canonical) != meta.Size {
		err := fmt.Errorf("reassembled size %d != original size %d", len(canonical), meta.Size)
		return fail(&report, "verification-failed", err), err
	}

	sum := sha256.Sum256(canonical)
	report.ActualSHA256 = hex.EncodeToString(sum[:])
	if report.ActualSHA256 != meta.SHA256 {
		err := errors.New("reassembled bytes do not match original SHA-256")
		return fail(&report, "hash-mismatch", err), err
	}
	report.Verified = true
	report.StatusSummary = "ok"
	return &DownloadResult{Report: report, Payload: canonical}, nil
}

func countIntervals(specs []rangespec.Spec, size int64) int {
	n := 0
	for _, s := range specs {
		if _, ok, err := rangespec.Resolve(s, size); err == nil && ok {
			n++
		}
	}
	return n
}

func fail(report *Report, summary string, err error) *DownloadResult {
	report.StatusSummary = summary
	report.FatalError = err.Error()
	return &DownloadResult{Report: *report}
}

// downloadGzip fetches the complete gzip representation, verifies the
// coded coverage (whole representation, one segment), then gunzips and
// verifies the canonical bytes.
func (c *Client) downloadGzip(ctx context.Context, report *Report, meta Metadata) (*DownloadResult, error) {
	body, hdr, status, attempts, err := c.request(ctx, http.MethodGet, report.URL, "", map[string]string{
		"Accept-Encoding": "gzip",
	})
	report.Attempts = attempts
	if err != nil {
		return fail(report, "download-failed", err), err
	}
	if status != http.StatusOK {
		err := fmt.Errorf("unexpected status %d for gzip representation", status)
		return fail(report, "reassembly-failed", err), err
	}
	if ce := hdr.Get("Content-Encoding"); ce != artifact.RepGzip {
		err := fmt.Errorf("expected Content-Encoding gzip, got %q", ce)
		return fail(report, "reassembly-failed", err), err
	}
	report.Encoding = artifact.RepGzip
	report.RepresentationSize = int64(len(body))
	report.ReassembledBytes = int64(len(body))
	report.Segments = []Segment{{
		First: 0, Last: int64(len(body)) - 1,
		Length: int64(len(body)), SHA256: hashHex(body),
	}}
	// Coverage is tracked against the coded representation: the server
	// reports its full coded length via Content-Length, which must match
	// the bytes actually delivered.
	if hdr.Get("Content-Length") != "" {
		if declared, perr := strconv.ParseInt(hdr.Get("Content-Length"), 10, 64); perr == nil && declared != int64(len(body)) {
			err := fmt.Errorf("coded length %d != delivered %d", declared, len(body))
			return fail(report, "incomplete-coverage", err), err
		}
	}
	canonical, derr := artifact.Gunzip(body)
	if derr != nil {
		return fail(report, "decode-failed", fmt.Errorf("gunzip representation: %w", derr)), derr
	}
	report.CompleteCoverage = len(canonical) == meta.Size
	report.ReassembledBytes = int64(len(body))
	if len(canonical) != meta.Size {
		err := fmt.Errorf("decoded size %d != original size %d", len(canonical), meta.Size)
		return fail(report, "verification-failed", err), err
	}
	sum := sha256.Sum256(canonical)
	report.ActualSHA256 = hex.EncodeToString(sum[:])
	if report.ActualSHA256 != meta.SHA256 {
		err := errors.New("gunzipped bytes do not match original SHA-256")
		return fail(report, "hash-mismatch", err), err
	}
	report.Verified = true
	report.StatusSummary = "ok"
	return &DownloadResult{Report: *report, Payload: canonical}, nil
}

func (c *Client) lookupMetadata(ctx context.Context, id string) (Metadata, error) {
	all, err := c.ListArtifacts(ctx)
	if err != nil {
		return Metadata{}, err
	}
	for _, m := range all {
		if m.ID == id {
			return m, nil
		}
	}
	return Metadata{}, fmt.Errorf("client: artifact %q not found", id)
}

func encodingOrIdentity(e string) string {
	if e == "" {
		return artifact.RepIdentity
	}
	return e
}

// request performs one logical HTTP request with bounded retries. It
// returns every attempt so the caller can embed them in its report.
func (c *Client) request(ctx context.Context, method, url, rangeHeader string, extra map[string]string) ([]byte, http.Header, int, []Attempt, error) {
	attempts := make([]Attempt, 0, MaxRetries)
	var lastErr error
	for attempt := 1; attempt <= MaxRetries; attempt++ {
		a := Attempt{Number: attempt, RangeHeader: rangeHeader}
		reqStart := c.clk.Now()

		req, err := http.NewRequestWithContext(ctx, method, url, nil)
		if err != nil {
			return nil, nil, 0, attempts, fmt.Errorf("building request: %w", err)
		}
		if rangeHeader != "" {
			req.Header.Set("Range", rangeHeader)
		}
		for k, v := range extra {
			req.Header.Set(k, v)
		}

		resp, err := c.http.Do(req)
		a.DurationMS = c.clk.Now().Sub(reqStart).Milliseconds()
		if err != nil {
			a.Error = err.Error()
			lastErr = err
		} else {
			a.Status = resp.StatusCode
			a.Decision = resp.Header.Get("X-Range-Decision")
			body, readErr := io.ReadAll(resp.Body)
			_ = resp.Body.Close()
			a.BytesReceived = int64(len(body))
			switch {
			case readErr != nil:
				a.Error = readErr.Error()
				lastErr = readErr
			case isRetryableStatus(resp.StatusCode):
				lastErr = fmt.Errorf("server responded %d", resp.StatusCode)
			default:
				attempts = append(attempts, a)
				return body, resp.Header, resp.StatusCode, attempts, nil
			}
		}

		if attempt < MaxRetries {
			a.RetryReason = lastErr.Error()
			attempts = append(attempts, a)
			if berr := c.clk.Sleep(ctx, defaultBackoff[attempt-1]); berr != nil {
				return nil, nil, 0, attempts, berr
			}
			continue
		}
		attempts = append(attempts, a)
	}
	return nil, nil, 0, attempts, fmt.Errorf("exhausted %d attempts: %w", MaxRetries, lastErr)
}

func isRetryableStatus(status int) bool {
	return status == http.StatusServiceUnavailable ||
		status == http.StatusBadGateway ||
		status == http.StatusGatewayTimeout ||
		status == http.StatusInternalServerError ||
		status == http.StatusTooManyRequests
}

func (c *Client) collectSingle(hdr http.Header, body []byte, report *Report, cov *coverage, assembly []byte) error {
	first, last, _, err := parseContentRange(hdr.Get("Content-Range"))
	if err != nil {
		return err
	}
	if int64(len(body)) != last-first+1 {
		return fmt.Errorf("truncated single range: Content-Range claims %d bytes, got %d", last-first+1, len(body))
	}
	overlap, err := cov.add(rangespec.Resolved{First: first, Last: last}, body, assembly)
	if err != nil {
		return err
	}
	report.Segments = append(report.Segments, Segment{
		First: first, Last: last, Length: int64(len(body)),
		SHA256: hashHex(body), Overlapping: overlap,
	})
	return nil
}

func (c *Client) collectMultipart(hdr http.Header, body []byte, report *Report, cov *coverage, assembly []byte) error {
	mt, params, err := mime.ParseMediaType(hdr.Get("Content-Type"))
	if err != nil || mt != "multipart/byteranges" {
		return fmt.Errorf("unexpected multipart content type %q: %v", hdr.Get("Content-Type"), err)
	}
	mr := multipart.NewReader(bytes.NewReader(body), params["boundary"])
	for {
		part, err := mr.NextPart()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return fmt.Errorf("reading multipart part: %w", err)
		}
		partBody, err := io.ReadAll(part)
		if err != nil {
			return fmt.Errorf("reading part body: %w", err)
		}
		first, last, _, perr := parseContentRange(part.Header.Get("Content-Range"))
		if perr != nil {
			return perr
		}
		if int64(len(partBody)) != last-first+1 {
			return fmt.Errorf("truncated part %d-%d: got %d bytes", first, last, len(partBody))
		}
		overlap, aerr := cov.add(rangespec.Resolved{First: first, Last: last}, partBody, assembly)
		if aerr != nil {
			return aerr
		}
		report.Segments = append(report.Segments, Segment{
			First: first, Last: last, Length: int64(len(partBody)),
			SHA256: hashHex(partBody), Overlapping: overlap,
		})
	}
	return nil
}

func placeFull(body []byte, report *Report, cov *coverage, assembly []byte) error {
	full := rangespec.Resolved{First: 0, Last: int64(len(body)) - 1}
	overlap, err := cov.add(full, body, assembly)
	if err != nil {
		return err
	}
	report.Segments = append(report.Segments, Segment{
		First: 0, Last: full.Last, Length: int64(len(body)),
		SHA256: hashHex(body), Overlapping: overlap,
	})
	return nil
}

// parseContentRange parses "bytes first-last/total" (total may be "*").
func parseContentRange(v string) (first, last, total int64, err error) {
	const prefix = "bytes "
	if !strings.HasPrefix(v, prefix) {
		return 0, 0, 0, fmt.Errorf("malformed Content-Range %q", v)
	}
	spec := strings.TrimPrefix(v, prefix)
	slash := strings.IndexByte(spec, '/')
	if slash < 0 {
		return 0, 0, 0, fmt.Errorf("malformed Content-Range %q", v)
	}
	if t := spec[slash+1:]; t != "*" {
		total, err = strconv.ParseInt(t, 10, 64)
		if err != nil {
			return 0, 0, 0, fmt.Errorf("malformed Content-Range total %q", v)
		}
	}
	dash := strings.IndexByte(spec[:slash], '-')
	if dash < 0 {
		return 0, 0, 0, fmt.Errorf("malformed Content-Range %q", v)
	}
	first, err = strconv.ParseInt(spec[:dash], 10, 64)
	if err != nil {
		return 0, 0, 0, fmt.Errorf("malformed Content-Range first %q", v)
	}
	last, err = strconv.ParseInt(spec[dash+1:slash], 10, 64)
	if err != nil || last < first {
		return 0, 0, 0, fmt.Errorf("malformed Content-Range interval %q", v)
	}
	return first, last, total, nil
}

func hashHex(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}
