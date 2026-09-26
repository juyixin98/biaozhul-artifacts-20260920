// Package fault injects transport-level failures into the HTTP client so the
// client's reassembly and integrity checks can be exercised against an
// otherwise correct in-process origin.
//
// Faults are deterministic and opt-in per RoundTrip; they never touch the
// origin process, which keeps the whole system self-contained.
package fault

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"net/http"
)

// Kind enumerates the injected faults.
type Kind string

// Supported fault kinds.
const (
	KindNone      Kind = ""              // well-behaved transport
	KindTruncate  Kind = "truncate-body" // body cut before Content-Length
	KindCorrupt   Kind = "corrupt-byte"  // one byte flipped in the body
	KindAbort     Kind = "abort"         // connection reset mid-body
	KindStripETag Kind = "strip-etag"    // ETag response header removed
)

// Config describes one fault application.
type Config struct {
	Kind Kind
	// AtByte is the body offset acted on for Truncate (cut point) and
	// Corrupt (flipped offset).
	AtByte int
}

// Transport decorates an http.RoundTripper. The zero-value Base defaults to
// http.DefaultTransport.
type Transport struct {
	Base   http.RoundTripper
	Config Config
}

// RoundTrip implements http.RoundTripper.
func (t *Transport) RoundTrip(req *http.Request) (*http.Response, error) {
	base := t.Base
	if base == nil {
		base = http.DefaultTransport
	}
	resp, err := base.RoundTrip(req)
	if err != nil || resp == nil {
		return resp, err
	}

	switch t.Config.Kind {
	case KindStripETag:
		resp.Header.Del("ETag")
	case KindTruncate:
		resp.Body = &faultBody{
			reader: io.LimitReader(resp.Body, int64(max(0, t.Config.AtByte))),
			closer: resp.Body,
			abort:  false,
		}
		// Content-Length now lies on purpose; reassembly must catch this.
	case KindAbort:
		resp.Body = &faultBody{
			reader: io.LimitReader(resp.Body, int64(max(0, t.Config.AtByte))),
			closer: resp.Body,
			abort:  true,
		}
	case KindCorrupt:
		body, readErr := io.ReadAll(resp.Body)
		_ = resp.Body.Close()
		if readErr != nil {
			return nil, fmt.Errorf("fault: cannot buffer body: %w", readErr)
		}
		if t.Config.AtByte < len(body) {
			body[t.Config.AtByte] ^= 0xFF
		}
		resp.Body = io.NopCloser(bytes.NewReader(body))
	}
	return resp, nil
}

// faultBody serves up to cutoff bytes, then returns either io.EOF (truncate)
// or io.ErrUnexpectedEOF (abort).
type faultBody struct {
	reader io.Reader
	closer io.Closer
	abort  bool
}

func (b *faultBody) Read(p []byte) (int, error) {
	n, err := b.reader.Read(p)
	if errors.Is(err, io.EOF) && b.abort {
		return n, io.ErrUnexpectedEOF
	}
	return n, err
}

func (b *faultBody) Close() error { return b.closer.Close() }
