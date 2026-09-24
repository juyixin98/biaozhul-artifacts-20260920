package parser

import (
	"strings"
	"testing"
)

// corpus is the set of streams used for exhaustive split verification.
// It mixes valid streams, pipelined streams and malformed/ambiguous streams;
// consistency must hold for all of them — error offsets are absolute and
// therefore split-independent.
func corpus() [][]byte {
	return [][]byte{
		// minimal bodyless
		req("GET / HTTP/1.1", "Host: example.com"),
		// Content-Length with body split-crossing bytes
		append(req("POST /a HTTP/1.1", "Host: x", "Content-Length: 11"), []byte("hello world")...),
		// zero-length CL
		req("POST /z HTTP/1.1", "Content-Length: 0"),
		// chunked, multiple chunks + trailers
		append(req("POST /c HTTP/1.1", "Transfer-Encoding: chunked"),
			[]byte("4\r\nWiki\r\n5\r\npedia\r\n1\r\n!\r\n0\r\nETag: x\r\n\r\n")...),
		// chunked with extensions
		append(req("POST /e HTTP/1.1", "Transfer-Encoding: chunked"),
			[]byte("3;a=b;c=\"d\"\r\nabc\r\n0\r\n\r\n")...),
		// pipelined: bodyless + CL + chunked
		func() []byte {
			b := req("GET /1 HTTP/1.0", "Host: a")
			b = append(b, req("POST /2 HTTP/1.1", "Content-Length: 3")...)
			b = append(b, []byte("abc")...)
			b = append(b, req("POST /3 HTTP/1.1", "Transfer-Encoding: chunked")...)
			b = append(b, []byte("2\r\nhi\r\n0\r\n\r\n")...)
			return b
		}(),
		// ambiguous / smuggling attempts: CL + chunked (both orders)
		req("POST /x HTTP/1.1", "Content-Length: 5", "Transfer-Encoding: chunked"),
		req("POST /x HTTP/1.1", "Transfer-Encoding: chunked", "Content-Length: 5"),
		// conflicting CLs
		req("POST /x HTTP/1.1", "Content-Length: 5", "Content-Length: 6"),
		// folded header
		[]byte("GET / HTTP/1.1\r\nX-A: v\r\n X-B: y\r\n\r\n"),
		// bare LF variants
		[]byte("GET / HTTP/1.1\nHost: x\r\n\r\n"),
		[]byte("POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\na\nb"),
		// truncated: mid request-line / headers / CL body / chunk
		[]byte("GET / HTTP/1.1\r\nHost: x\r\n"),
		append(req("POST /x HTTP/1.1", "Content-Length: 9"), []byte("abc")...),
		append(req("POST /x HTTP/1.1", "Transfer-Encoding: chunked"), []byte("5\r\nabc")...),
		append(req("POST /x HTTP/1.1", "Transfer-Encoding: chunked"), []byte("5\r\nhelloX")...),
		// invalid CL characters
		req("POST /x HTTP/1.1", "Content-Length: 0x10"),
		req("POST /x HTTP/1.1", "Content-Length: 1 2"),
		// invalid chunk size
		append(req("POST /x HTTP/1.1", "Transfer-Encoding: chunked"), []byte("zz\r\n")...),
		// bad header name / value
		[]byte("GET / HTTP/1.1\r\nX Bad: v\r\n\r\n"),
		[]byte("GET / HTTP/1.1\r\nX-A: v\x00\r\n\r\n"),
		// boundary edge cases
		nil,
		[]byte("\r\n"),
	}
}

func TestVerifyAllSplits(t *testing.T) {
	for i, input := range corpus() {
		if m := VerifyAllSplits(input); m != nil {
			t.Fatalf("corpus[%d] (%d bytes, head=%q): %v", i, len(input), head(input), m)
		}
	}
}

func TestVerifyAllSplitsWithLimit(t *testing.T) {
	opts := []Option{WithMaxBody(4)}
	for i, input := range corpus() {
		if m := VerifyAllSplits(input, opts...); m != nil {
			t.Fatalf("corpus[%d] inconsistent under maxbody=4: %v", i, m)
		}
	}
}

func TestOneByteFeeds(t *testing.T) {
	// Feed every single byte in its own call; result must equal Parse.
	for i, input := range corpus() {
		p := NewParser()
		var got []*Request
		var fErr *Error
		for j := 0; j < len(input); j++ {
			rs, e := p.Feed(input[j : j+1])
			got = append(got, rs...)
			if e != nil {
				fErr = e
				break
			}
		}
		if fErr == nil {
			fErr = p.Finish()
		}
		want, wErr := Parse(input)
		if (fErr == nil) != (wErr == nil) {
			t.Fatalf("corpus[%d]: error mismatch one-byte=%v oneshot=%v", i, fErr, wErr)
		}
		if fErr != nil && (fErr.Code != wErr.Code || fErr.Offset != wErr.Offset) {
			t.Fatalf("corpus[%d]: error detail one-byte=%v oneshot=%v", i, fErr, wErr)
		}
		if fErr == nil && len(got) != len(want) {
			t.Fatalf("corpus[%d]: request count %d vs %d", i, len(got), len(want))
		}
	}
}

func TestRandomMultiSplits(t *testing.T) {
	// Larger inputs where exhaustive O(n^2) checking is wasteful.
	big := make([]byte, 0, 20000)
	for k := 0; k < 50; k++ {
		big = append(big, req("POST /big HTTP/1.1", "Content-Length: 200")...)
		big = append(big, make([]byte, 200)...)
	}
	if m := VerifyRandomSplits(big, 42, 20); m != nil {
		t.Fatalf("pipelined CL stream inconsistent: %v", m)
	}

	var chunks strings.Builder
	for k := 0; k < 100; k++ {
		chunks.WriteString("a\r\n0123456789\r\n")
	}
	big2 := append(req("POST /big HTTP/1.1", "Transfer-Encoding: chunked"), []byte(chunks.String())...)
	big2 = append(big2, []byte("0\r\n\r\n")...)
	if m := VerifyRandomSplits(big2, 7, 20); m != nil {
		t.Fatalf("chunked stream inconsistent: %v", m)
	}
}

func TestStreamingIntermediateCompleteness(t *testing.T) {
	// The first pipelined request must be reported complete as soon as its
	// bytes have been fed, even if the second request is still partial.
	good := req("GET /1 HTTP/1.1", "Host: x")
	partial := []byte("POST /2 HTTP/1.1\r\nContent-Length: 10\r\n\r\nabc")
	input := append(append([]byte{}, good...), partial...)

	p := NewParser()
	rs, e := p.Feed(input)
	if e != nil {
		t.Fatalf("unexpected early error: %v", e)
	}
	if len(rs) != 1 || rs[0].Target != "/1" {
		t.Fatalf("expected exactly 1 completed request, got %+v", rs)
	}
	if err := p.Finish(); err == nil || err.Code != ErrBodyTruncated {
		t.Fatalf("expected body_truncated at Finish, got %v", err)
	}
}

func head(b []byte) string {
	if len(b) > 24 {
		return string(b[:24])
	}
	return string(b)
}
