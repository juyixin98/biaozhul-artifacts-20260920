// Package client is the fault-injecting HTTP client used to validate the
// range service. It issues Range/If-Range requests against the in-process
// origin, parses single and multipart 206 responses, reassembles the bytes,
// and verifies them against the representation it fetched separately.
package client

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"

	"httprange/internal/fault"
)

// Client talks to one range-service base URL.
type Client struct {
	baseURL    string
	httpClient *http.Client
}

// New builds a client. baseURL is e.g. http://127.0.0.1:8080.
func New(baseURL string) *Client {
	return &Client{
		baseURL:    strings.TrimRight(baseURL, "/"),
		httpClient: &http.Client{},
	}
}

// SetFault installs a fault-injecting transport for subsequent requests.
func (c *Client) SetFault(cfg fault.Config) {
	c.httpClient.Transport = &fault.Transport{Config: cfg}
}

// ClearFault removes fault injection.
func (c *Client) ClearFault() {
	c.httpClient.Transport = nil
}

// Representation is a complete representation fetched from the origin, used
// as ground truth during reassembly verification.
type Representation struct {
	Body        []byte
	ETag        string
	ContentType string
	Encoding    string
	Headers     http.Header
}

// FetchRepresentation issues a plain GET and returns the full representation.
// acceptEncoding ("", "gzip", ...) is sent verbatim so gzip responses are
// returned encoded rather than transparently decompressed by the transport.
func (c *Client) FetchRepresentation(id, acceptEncoding string) (*Representation, error) {
	resp, err := c.doGet(id, nil, acceptEncoding)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("fetch representation: unexpected status %d", resp.StatusCode)
	}
	body, err := readChecked(resp)
	if err != nil {
		return nil, err
	}
	return &Representation{
		Body:        body,
		ETag:        resp.Header.Get("ETag"),
		ContentType: resp.Header.Get("Content-Type"),
		Encoding:    resp.Header.Get("Content-Encoding"),
		Headers:     resp.Header.Clone(),
	}, nil
}

// RangeOutcome classifies a range response.
type RangeOutcome struct {
	StatusCode  int
	ETag        string
	ContentType string
	// FullBody is set when the server answers 200 (range ignored / If-Range
	// mismatch / malformed header).
	FullBody []byte
	// Single is set for a one-range 206.
	Single *RangePart
	// Parts is set for a multipart 206.
	Parts []RangePart
	// Body holds the raw body for non-206 responses (e.g. 416/200).
	Body []byte
	// ContentLength observed on the wire.
	ContentLength int64
	// ContentRange records the Content-Range header (set on 206 and 416).
	ContentRange string
	// Headers carries selected raw response headers for reporting.
	Headers http.Header
}

// FetchRanges sends "bytes=<spec>" with optional If-Range and
// Accept-Encoding headers.
func (c *Client) FetchRanges(id, spec, ifRange, acceptEncoding string) (*RangeOutcome, error) {
	headers := map[string]string{"Range": "bytes=" + spec}
	if ifRange != "" {
		headers["If-Range"] = ifRange
	}
	resp, err := c.doGet(id, headers, acceptEncoding)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	out := &RangeOutcome{
		StatusCode:    resp.StatusCode,
		ETag:          resp.Header.Get("ETag"),
		ContentType:   resp.Header.Get("Content-Type"),
		ContentLength: resp.ContentLength,
		ContentRange:  resp.Header.Get("Content-Range"),
		Headers:       resp.Header.Clone(),
	}

	switch {
	case resp.StatusCode == http.StatusOK:
		body, rerr := readChecked(resp)
		if rerr != nil {
			return nil, rerr
		}
		out.FullBody = body
		out.Body = body
		return out, nil
	case resp.StatusCode == http.StatusPartialContent:
		body, rerr := io.ReadAll(resp.Body)
		if rerr != nil {
			return nil, fmt.Errorf("read 206 body: %w", rerr)
		}
		out.Body = body
		if strings.HasPrefix(out.ContentType, "multipart/byteranges") {
			parts, perr := parseMultipart(body, out.ContentType)
			if perr != nil {
				return nil, perr
			}
			out.Parts = parts
			return out, nil
		}
		cr := resp.Header.Get("Content-Range")
		start, end, total, perr := parseContentRange(cr)
		if perr != nil {
			return nil, perr
		}
		if int64(len(body)) != end-start+1 {
			return nil, fmt.Errorf("%w: single-part length %d != %d-%d",
				ErrMalformedMultipart, len(body), start, end)
		}
		out.Single = &RangePart{Start: start, End: end, Total: total, Payload: body}
		return out, nil
	case resp.StatusCode == http.StatusRequestedRangeNotSatisfiable:
		body, _ := io.ReadAll(resp.Body)
		out.Body = body
		return out, nil
	default:
		return nil, fmt.Errorf("unexpected status %d", resp.StatusCode)
	}
}

// FetchRangesRaw sends a raw Range header value (e.g. "items=0-9") plus
// optional extra headers, used to exercise malformed/unsupported ranges.
func (c *Client) FetchRangesRaw(id, rawRange string, extra map[string]string) (*RangeOutcome, error) {
	headers := map[string]string{"Range": rawRange}
	for k, v := range extra {
		headers[k] = v
	}
	resp, err := c.doGet(id, headers, "")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	out := &RangeOutcome{
		StatusCode:    resp.StatusCode,
		ETag:          resp.Header.Get("ETag"),
		ContentType:   resp.Header.Get("Content-Type"),
		ContentLength: resp.ContentLength,
		ContentRange:  resp.Header.Get("Content-Range"),
		Headers:       resp.Header.Clone(),
	}
	switch {
	case resp.StatusCode == http.StatusOK:
		body, rerr := readChecked(resp)
		if rerr != nil {
			return nil, rerr
		}
		out.FullBody = body
		out.Body = body
	case resp.StatusCode == http.StatusRequestedRangeNotSatisfiable:
		out.Body, _ = io.ReadAll(resp.Body)
	default:
		return nil, fmt.Errorf("unexpected status %d", resp.StatusCode)
	}
	return out, nil
}

// doGet performs a GET against /artifacts/id with extra headers.
func (c *Client) doGet(id string, headers map[string]string, acceptEncoding string) (*http.Response, error) {
	req, err := http.NewRequest(http.MethodGet, c.baseURL+"/artifacts/"+id, nil)
	if err != nil {
		return nil, err
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	if acceptEncoding != "" {
		req.Header.Set("Accept-Encoding", acceptEncoding)
	}
	return c.httpClient.Do(req)
}

// readChecked reads the body and, when Content-Length is known, rejects a
// short read caused by truncation faults.
func readChecked(resp *http.Response) ([]byte, error) {
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read body: %w", err)
	}
	if resp.ContentLength >= 0 && int64(len(body)) != resp.ContentLength {
		return nil, fmt.Errorf("%w: got %d bytes, Content-Length %d",
			ErrTruncated, len(body), resp.ContentLength)
	}
	return body, nil
}

// ErrTruncated indicates the delivered body was shorter than declared.
var ErrTruncated = errors.New("response body truncated")

// Reassemble places parts into a size-byte buffer. It rejects gaps, overlaps
// and out-of-bounds offsets, returning the reconstructed representation only
// when every position is covered exactly once.
func Reassemble(parts []RangePart, size int64) ([]byte, error) {
	buf := make([]byte, size)
	filled := make([]bool, size)
	for _, p := range parts {
		if p.Start < 0 || p.End >= size || p.End < p.Start {
			return nil, fmt.Errorf("part %d-%d out of bounds for size %d", p.Start, p.End, size)
		}
		if int64(len(p.Payload)) != p.End-p.Start+1 {
			return nil, fmt.Errorf("part %d-%d payload length mismatch", p.Start, p.End)
		}
		for i := p.Start; i <= p.End; i++ {
			if filled[i] {
				return nil, fmt.Errorf("%w: overlap at byte %d", ErrOverlap, i)
			}
			filled[i] = true
			buf[i] = p.Payload[i-p.Start]
		}
	}
	for i, ok := range filled {
		if !ok {
			return nil, fmt.Errorf("%w: gap at byte %d", ErrGap, i)
		}
	}
	return buf, nil
}

// ErrOverlap and ErrGap classify reassembly failures.
var (
	ErrOverlap = errors.New("reassembly overlap")
	ErrGap     = errors.New("reassembly gap")
)

// VerifyParts checks every part's payload against the ground-truth bytes at
// its declared offsets. Unlike Reassemble it tolerates overlaps (each copy
// must still be byte-correct), and reports the set of uncovered offsets via
// the returned Coverage: callers decide whether gaps are acceptable.
func VerifyParts(parts []RangePart, original []byte) (Coverage, error) {
	size := int64(len(original))
	cov := Coverage{Size: size, covered: make([]bool, size)}
	for _, p := range parts {
		if p.Start < 0 || p.End >= size || p.End < p.Start {
			return Coverage{}, fmt.Errorf("part %d-%d out of bounds for size %d",
				p.Start, p.End, size)
		}
		if int64(len(p.Payload)) != p.End-p.Start+1 {
			return Coverage{}, fmt.Errorf("part %d-%d payload length mismatch", p.Start, p.End)
		}
		for i := p.Start; i <= p.End; i++ {
			if p.Payload[i-p.Start] != original[i] {
				return Coverage{}, fmt.Errorf("%w: byte %d differs from ground truth",
					ErrIntegrity, i)
			}
			cov.covered[i] = true
		}
	}
	for _, ok := range cov.covered {
		if !ok {
			cov.Gaps++
		}
	}
	return cov, nil
}

// Coverage summarizes how much of a representation the parts span.
type Coverage struct {
	Size    int64
	Gaps    int64
	covered []bool
}

// Complete reports whether every offset is covered at least once.
func (c Coverage) Complete() bool { return c.Gaps == 0 }

// Verify compares reconstructed bytes with the ground-truth representation.
func Verify(reconstructed, original []byte) error {
	if len(reconstructed) != len(original) {
		return fmt.Errorf("%w: length %d != original %d",
			ErrIntegrity, len(reconstructed), len(original))
	}
	if !bytes.Equal(reconstructed, original) {
		return fmt.Errorf("%w: byte mismatch", ErrIntegrity)
	}
	return nil
}

// ErrIntegrity indicates failed end-to-end verification.
var ErrIntegrity = errors.New("integrity check failed")

// SHA256 renders a lowercase hex digest for reporting.
func SHA256(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}
