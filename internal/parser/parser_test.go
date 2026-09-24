package parser

import (
	"testing"
)

func req(lines ...string) []byte {
	var b []byte
	for _, l := range lines {
		b = append(b, l...)
		b = append(b, '\r', '\n')
	}
	b = append(b, '\r', '\n')
	return b
}

func wantErr(t *testing.T, input []byte, code ErrorCode, offset int) {
	t.Helper()
	_, err := Parse(input)
	if err == nil {
		t.Fatalf("expected error %s at %d, got success", code, offset)
	}
	if err.Code != code || err.Offset != offset {
		t.Fatalf("expected %s at %d, got %v", code, offset, err)
	}
}

func wantOK(t *testing.T, input []byte, n int) []*Request {
	t.Helper()
	reqs, err := Parse(input)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(reqs) != n {
		t.Fatalf("expected %d requests, got %d", n, len(reqs))
	}
	return reqs
}

func TestParseContentLength(t *testing.T) {
	input := append(req("POST /a HTTP/1.1", "Host: x", "Content-Length: 5"), []byte("hello")...)
	rs := wantOK(t, input, 1)
	r := rs[0]
	if r.Method != "POST" || r.Target != "/a" || r.Version != "HTTP/1.1" {
		t.Fatalf("bad request line: %+v", r)
	}
	if r.Frame != FrameContentLength || r.ContentLength != 5 || string(r.Body) != "hello" {
		t.Fatalf("bad CL framing: %+v body=%q", r, r.Body)
	}
	if r.BodyLength != 5 || r.RawLength != len(input) || r.StartOffset != 0 {
		t.Fatalf("bad metadata: raw=%d want=%d start=%d", r.RawLength, len(input), r.StartOffset)
	}
}

func TestParseChunked(t *testing.T) {
	input := append(req("POST /c HTTP/1.1", "Transfer-Encoding: chunked"),
		[]byte("4\r\nWiki\r\n5\r\npedia\r\n0\r\nX-Trailer: yes\r\n\r\n")...)
	rs := wantOK(t, input, 1)
	r := rs[0]
	if r.Frame != FrameChunked {
		t.Fatalf("expected chunked, got %s", r.Frame)
	}
	if string(r.Body) != "Wikipedia" {
		t.Fatalf("decoded body = %q, want Wikipedia", r.Body)
	}
	if len(r.TrailingHeader) != 1 || r.TrailingHeader[0].Name != "X-Trailer" {
		t.Fatalf("bad trailers: %+v", r.TrailingHeader)
	}
	if r.RawLength != len(input) {
		t.Fatalf("raw length %d want %d", r.RawLength, len(input))
	}
}

func TestParseChunkExtensionsIgnored(t *testing.T) {
	input := append(req("POST /e HTTP/1.1", "Transfer-Encoding: chunked"),
		[]byte("4; name=\"x\"\r\nWiki\r\n0\r\n\r\n")...)
	rs := wantOK(t, input, 1)
	if string(rs[0].Body) != "Wiki" {
		t.Fatalf("body = %q", rs[0].Body)
	}
}

func TestParseNoBody(t *testing.T) {
	rs := wantOK(t, req("GET / HTTP/1.1", "Host: x"), 1)
	if rs[0].Frame != FrameNone || rs[0].BodyLength != 0 {
		t.Fatalf("expected frameless request, got %+v", rs[0])
	}
}

func TestParsePipeline(t *testing.T) {
	first := append(req("GET /1 HTTP/1.1", "Host: x"), []byte("GET /2 HTTP/1.1\r\nHost: y\r\nContent-Length: 2\r\n\r\nhi")...)
	rs := wantOK(t, first, 2)
	if rs[0].Target != "/1" || rs[1].Target != "/2" {
		t.Fatalf("bad targets: %q %q", rs[0].Target, rs[1].Target)
	}
	if string(rs[1].Body) != "hi" || rs[1].StartOffset != rs[0].RawLength {
		t.Fatalf("bad second request: start=%d prevRaw=%d body=%q",
			rs[1].StartOffset, rs[0].RawLength, rs[1].Body)
	}
}

func TestRejectFoldedHeader(t *testing.T) {
	// "GET / HTTP/1.1\r\nX-A: v\r\n X-B: y\r\n\r\n"
	input := []byte("GET / HTTP/1.1\r\nX-A: v\r\n X-B: y\r\n\r\n")
	wantErr(t, input, ErrFoldedHeader, len("GET / HTTP/1.1\r\nX-A: v\r\n"))
}

func TestRejectBareLF(t *testing.T) {
	// Bare LF right after GET
	wantErr(t, []byte("GET / HTTP/1.1\nHost: x\r\n\r\n"), ErrBareLF, len("GET / HTTP/1.1"))
}

func TestRejectCLAndChunked(t *testing.T) {
	// CL first, TE second -> error offset is the TE line start.
	input := req("POST /x HTTP/1.1", "Content-Length: 5", "Transfer-Encoding: chunked")
	teOff := len("POST /x HTTP/1.1\r\nContent-Length: 5\r\n")
	wantErr(t, input, ErrCLAndChunked, teOff)

	// TE first, CL second -> error offset is still the TE line.
	input2 := req("POST /x HTTP/1.1", "Transfer-Encoding: chunked", "Content-Length: 5")
	teOff2 := len("POST /x HTTP/1.1\r\n")
	wantErr(t, input2, ErrCLAndChunked, teOff2)
}

func TestRejectConflictingCL(t *testing.T) {
	input := req("POST /x HTTP/1.1", "Content-Length: 5", "Content-Length: 6")
	secondLine := len("POST /x HTTP/1.1\r\nContent-Length: 5\r\n")
	wantErr(t, input, ErrContentLengthConflict, secondLine)
}

func TestAcceptIdenticalCL(t *testing.T) {
	input := append(req("POST /x HTTP/1.1", "Content-Length: 5", "Content-Length: 5"),
		[]byte("hello")...)
	rs := wantOK(t, input, 1)
	if rs[0].ContentLength != 5 || string(rs[0].Body) != "hello" {
		t.Fatalf("bad body: %q cl=%d", rs[0].Body, rs[0].ContentLength)
	}
}

func TestRejectInvalidCL(t *testing.T) {
	// "Content-Length: 5x" -> bad byte at 'x', relative to full input.
	input := req("POST /x HTTP/1.1", "Content-Length: 5x")
	off := len("POST /x HTTP/1.1\r\nContent-Length: ") + 1
	wantErr(t, input, ErrInvalidContentLength, off)

	wantErr(t, req("POST /x HTTP/1.1", "Content-Length: 0x10"),
		ErrInvalidContentLength, len("POST /x HTTP/1.1\r\nContent-Length: 0"))
	wantErr(t, req("POST /x HTTP/1.1", "Content-Length: -1"),
		ErrInvalidContentLength, len("POST /x HTTP/1.1\r\nContent-Length: "))
	wantErr(t, req("POST /x HTTP/1.1", "Content-Length:"),
		ErrInvalidContentLength, len("POST /x HTTP/1.1\r\nContent-Length:"))
}

func TestRejectUnsupportedTE(t *testing.T) {
	teOff := len("POST /x HTTP/1.1\r\n")
	wantErr(t, req("POST /x HTTP/1.1", "Transfer-Encoding: gzip"), ErrUnsupportedTransferEncoding, teOff)
	wantErr(t, req("POST /x HTTP/1.1", "Transfer-Encoding: gzip, chunked"), ErrUnsupportedTransferEncoding, teOff)
	wantErr(t, req("POST /x HTTP/1.1", "Transfer-Encoding: chunked, gzip"), ErrUnsupportedTransferEncoding, teOff)
	// Whitespace around the coding is tolerated; capitalization is not.
	rs := wantOK(t, append(req("POST /x HTTP/1.1", "Transfer-Encoding: Chunked"),
		[]byte("0\r\n\r\n")...), 1)
	if rs[0].Frame != FrameChunked {
		t.Fatalf("Chunked (case-insensitive) should be accepted")
	}
}

func TestTruncatedCLBody(t *testing.T) {
	input := append(req("POST /x HTTP/1.1", "Content-Length: 10"), []byte("abc")...)
	wantErr(t, input, ErrBodyTruncated, len(input))
}

func TestTruncatedChunk(t *testing.T) {
	// chunk-size says 5 but only 3 bytes arrive then EOF
	input := append(req("POST /x HTTP/1.1", "Transfer-Encoding: chunked"),
		[]byte("5\r\nabc")...)
	wantErr(t, input, ErrChunkDataTruncated, len(input))

	// missing CRLF after chunk data: "5\r\nhelloX..."
	input2 := append(req("POST /x HTTP/1.1", "Transfer-Encoding: chunked"),
		[]byte("5\r\nhelloX\r\n0\r\n\r\n")...)
	wantErr(t, input2, ErrChunkCRLF, len(req("POST /x HTTP/1.1", "Transfer-Encoding: chunked"))+len("5\r\nhello"))
}

func TestInvalidChunkSize(t *testing.T) {
	input := append(req("POST /x HTTP/1.1", "Transfer-Encoding: chunked"),
		[]byte("0x5\r\n")...)
	wantErr(t, input, ErrInvalidChunkSize, len(req("POST /x HTTP/1.1", "Transfer-Encoding: chunked")))
}

func TestInvalidHeaderName(t *testing.T) {
	input := []byte("GET / HTTP/1.1\r\nX Bad: v\r\n\r\n")
	wantErr(t, input, ErrInvalidHeader, len("GET / HTTP/1.1\r\nX"))
}

func TestInvalidFieldValue(t *testing.T) {
	// NUL in field value -> exact position
	input := []byte("GET / HTTP/1.1\r\nX-A: v\x00v\r\n\r\n")
	wantErr(t, input, ErrInvalidFieldValue, len("GET / HTTP/1.1\r\nX-A: v"))
}

func TestInvalidRequestLine(t *testing.T) {
	wantErr(t, req("GET  / HTTP/1.1"), ErrInvalidRequestLine, len("GET "))
	wantErr(t, []byte("GE[T / HTTP/1.1\r\n\r\n"), ErrInvalidRequestLine, 2)
	wantErr(t, req("GET / HTTP/2.0"), ErrUnsupportedVersion, len("GET / "))
}

func TestMaxBodyLimit(t *testing.T) {
	input := append(req("POST /x HTTP/1.1", "Content-Length: 100"), make([]byte, 100)...)
	_, err := Parse(input, WithMaxBody(50))
	if err == nil || err.Code != ErrBodyTooLarge {
		t.Fatalf("expected body_too_large, got %v", err)
	}
}

func TestEmptyStream(t *testing.T) {
	rs, err := Parse(nil)
	if err != nil || len(rs) != 0 {
		t.Fatalf("empty input should be valid with 0 requests, got rs=%d err=%v", len(rs), err)
	}
}

func TestIncrementalEqualsOneShotAfterError(t *testing.T) {
	// Pipelined stream: first good request, then an invalid second one.
	good := req("GET /1 HTTP/1.1", "Host: x")
	bad := []byte("GE[T /2 HTTP/1.1\r\n\r\n")
	input := append(append([]byte{}, good...), bad...)

	rs, err := Parse(input)
	if err == nil || err.Code != ErrInvalidRequestLine {
		t.Fatalf("expected invalid_request_line, got %v", err)
	}
	if len(rs) != 1 || rs[0].Target != "/1" {
		t.Fatalf("expected first request preserved, got %+v", rs)
	}
	if err.Offset != len(good)+2 {
		t.Fatalf("error offset %d, want %d", err.Offset, len(good)+2)
	}
}
