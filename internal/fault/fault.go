// Package fault contains in-process fault injection used by the loader
// tests. Failures are scripted and deterministic: the transport consumes a
// Fault on each request until the script is exhausted, then everything
// passes through untouched.
package fault

import (
	"errors"
	"io"
	"net/http"
)

// Kind enumerates the injected failure modes.
type Kind int

const (
	// KindNone is a script no-op: the request passes through. It lets a
	// script place a fault on the 2nd or 3rd request of a sequence.
	KindNone Kind = iota
	// KindTransportError makes RoundTrip return a synthetic network error
	// without ever reaching the server.
	KindTransportError
	// KindStatus503 makes the server response look like a 503.
	KindStatus503
	// KindStatus500 makes the server response look like a 500.
	KindStatus500
	// KindTruncated closes the body early (simulated via a short body).
	KindTruncated
)

// Fault is one scripted failure.
type Fault struct {
	Kind Kind
}

// ScriptedTransport wraps an http.RoundTripper and fails according to an
// ordered script. It is safe for concurrent use in the sense required by
// the tests: the client issues sequential requests per download.
type ScriptedTransport struct {
	Inner    http.RoundTripper
	script   []Fault
	consumed int
}

// NewScriptedTransport builds a transport from t with the given faults in
// effect for the first len(faults) requests.
func NewScriptedTransport(t http.RoundTripper, faults []Fault) *ScriptedTransport {
	cp := make([]Fault, len(faults))
	copy(cp, faults)
	return &ScriptedTransport{Inner: t, script: cp}
}

// ErrInjected is the synthetic network error returned for KindTransportError.
var ErrInjected = errors.New("fault: injected transport failure")

func (s *ScriptedTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	if s.consumed < len(s.script) {
		f := s.script[s.consumed]
		s.consumed++
		switch f.Kind {
		case KindNone:
			return s.Inner.RoundTrip(req)
		case KindTransportError:
			return nil, ErrInjected
		case KindStatus503:
			return syntheticResponse(req, http.StatusServiceUnavailable), nil
		case KindStatus500:
			return syntheticResponse(req, http.StatusInternalServerError), nil
		case KindTruncated:
			resp, err := s.Inner.RoundTrip(req)
			if err != nil {
				return nil, err
			}

			resp.Body = &truncatedBody{inner: resp.Body}
			resp.ContentLength = -1
			resp.Header.Del("Content-Length")
			return resp, nil
		}
	}
	return s.Inner.RoundTrip(req)
}

// Consumed reports how many scripted faults have fired.
func (s *ScriptedTransport) Consumed() int { return s.consumed }

func syntheticResponse(req *http.Request, status int) *http.Response {
	return &http.Response{
		Status:     http.StatusText(status),
		StatusCode: status,
		Proto:      "HTTP/1.1",
		ProtoMajor: 1,
		ProtoMinor: 1,
		Header: http.Header{
			"Content-Type": []string{"application/json"},
		},
		Body:          http.NoBody,
		ContentLength: 0,
		Request:       req,
	}
}

type truncatedBody struct {
	inner io.ReadCloser
}

func (b *truncatedBody) Read(p []byte) (int, error) {
	return 0, errors.New("fault: injected unexpected EOF")
}

func (b *truncatedBody) Close() error { return b.inner.Close() }
