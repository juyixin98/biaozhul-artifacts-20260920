package frame

import (
	"bytes"
	"fmt"
	"strings"
	"testing"
)

func mustParse(t *testing.T, data []byte, opts ...Option) *Message {
	t.Helper()
	m, e := Parse(data, opts...)
	if e != nil {
		t.Fatalf("unexpected error: %v\nraw=%q", e, data)
	}
	return m
}

func TestParseContentLength(t *testing.T) {
	raw := []byte("POST /a HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhello")
	m := mustParse(t, raw)
	if m.Method != "POST" || m.Target != "/a" {
		t.Fatalf("bad request line: %+v", m)
	}
	if string(m.Body) != "hello" || m.BodyLength != 5 {
		t.Fatalf("bad body: %q len=%d", m.Body, m.BodyLength)
	}
	if m.Chunked {
		t.Fatal("must not be chunked")
	}
	// HeaderEnd points at body start; raw[40:] = "hello"
	if raw[m.HeaderEnd] != 'h' {
		t.Fatalf("HeaderEnd=%d wrong", m.HeaderEnd)
	}
	if m.End != len(raw) {
		t.Fatalf("End=%d want %d", m.End, len(raw))
	}
}

func TestParseNoBody(t *testing.T) {
	raw := []byte("GET / HTTP/1.1\r\nHost: x\r\n\r\n")
	m := mustParse(t, raw)
	if len(m.Body) != 0 || m.End != len(raw) {
		t.Fatalf("bad empty-body message: %+v", m)
	}
}

func TestParseChunked(t *testing.T) {
	raw := []byte("POST /c HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n" +
		"5;foo=bar\r\nhello\r\n" +
		"6\r\n world\r\n" +
		"0\r\nX-Trailer: v\r\n\r\n")
	m := mustParse(t, raw)
	if !m.Chunked {
		t.Fatal("want chunked")
	}
	if string(m.Body) != "hello world" {
		t.Fatalf("body=%q", m.Body)
	}
	if len(m.Chunks) != 2 {
		t.Fatalf("chunks=%d", len(m.Chunks))
	}
	if m.Chunks[0].Size != 5 || m.Chunks[0].Extension != "foo=bar" ||
		string(m.Chunks[0].Data) != "hello" {
		t.Fatalf("chunk0=%+v", m.Chunks[0])
	}
	if m.Chunks[1].Size != 6 || string(m.Chunks[1].Data) != " world" {
		t.Fatalf("chunk1=%+v", m.Chunks[1])
	}
	if len(m.Trailers) != 1 || m.Trailers[0].Name != "X-Trailer" {
		t.Fatalf("trailers=%+v", m.Trailers)
	}
}

func TestParseChunkedNoTrailers(t *testing.T) {
	raw := []byte("POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n2\r\nhi\r\n0\r\n\r\n")
	m := mustParse(t, raw)
	if string(m.Body) != "hi" || len(m.Chunks) != 1 || len(m.Trailers) != 0 {
		t.Fatalf("got body=%q chunks=%d trailers=%d", m.Body, len(m.Chunks), len(m.Trailers))
	}
}

func TestPipeline(t *testing.T) {
	raw := []byte("GET /1 HTTP/1.1\r\nHost: a\r\n\r\n" +
		"POST /2 HTTP/1.1\r\nContent-Length: 3\r\n\r\nabc" +
		"GET /3 HTTP/1.1\r\nHost: c\r\n\r\n")
	msgs, e := ParsePipeline(raw)
	if e != nil {
		t.Fatal(e)
	}
	if len(msgs) != 3 {
		t.Fatalf("got %d messages", len(msgs))
	}
	wantStarts := []int{0, bytes.Index(raw, []byte("POST /2")), bytes.Index(raw, []byte("GET /3"))}
	for i, m := range msgs {
		if m.Start != wantStarts[i] {
			t.Fatalf("msg %d start=%d want %d", i, m.Start, wantStarts[i])
		}
	}
	if string(msgs[1].Body) != "abc" {
		t.Fatalf("body=%q", msgs[1].Body)
	}
}

func TestSingleRejectsTrailingBytes(t *testing.T) {
	raw := []byte("GET / HTTP/1.1\r\nHost: x\r\n\r\nGET /2 HTTP/1.1\r\nHost: y\r\n\r\n")
	_, e := Parse(raw)
	if e == nil || e.Code != ErrTooManyMessages {
		t.Fatalf("got %v", e)
	}
	// offset must point at the second request's first byte
	if e.Offset != bytes.Index(raw, []byte("GET /2")) {
		t.Fatalf("offset=%d", e.Offset)
	}
}

type errCase struct {
	name   string
	raw    string
	code   string
	offset int // -1 means "don't assert exact offset"
}

func errCases() []errCase {
	clChunked := "POST / HTTP/1.1\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\nx\r\n0\r\n\r\n"
	chunkedCL := "POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\nContent-Length: 1\r\n\r\nx\r\n0\r\n\r\n"
	return []errCase{
		{"cl_and_chunked", clChunked, ErrCLAndChunked, strings.Index(clChunked, "Transfer-Encoding")},
		{"chunked_and_cl", chunkedCL, ErrCLAndChunked, strings.Index(chunkedCL, "Content-Length")},
		{"conflicting_cl", "POST / HTTP/1.1\r\nContent-Length: 2\r\nContent-Length: 3\r\n\r\nab",
			ErrContentLenConflict, strings.Index("POST / HTTP/1.1\r\nContent-Length: 2\r\nContent-Length: 3\r\n\r\nab", "Content-Length: 3")},
		{"comma_cl", "POST / HTTP/1.1\r\nContent-Length: 2, 3\r\n\r\nab",
			ErrBadContentLength, strings.Index("POST / HTTP/1.1\r\nContent-Length: 2, 3\r\n\r\nab", "Content-Length")},
		{"nonnumeric_cl", "POST / HTTP/1.1\r\nContent-Length: 2x\r\n\r\nab",
			ErrBadContentLength, strings.Index("POST / HTTP/1.1\r\nContent-Length: 2x\r\n\r\nab", "Content-Length")},
		{"folded_header", "POST / HTTP/1.1\r\nContent-Length: 2\r\n\tX: y\r\n\r\nab",
			ErrFoldedHeader, strings.Index("POST / HTTP/1.1\r\nContent-Length: 2\r\n\tX: y\r\n\r\nab", "\r\n\tX") + 2},
		{"folded_space", "POST / HTTP/1.1\r\nX: a\r\n b\r\n\r\n",
			ErrFoldedHeader, strings.Index("POST / HTTP/1.1\r\nX: a\r\n b\r\n\r\n", "\r\n b") + 2},
		{"bare_lf", "POST / HTTP/1.1\nHost: x\r\n\r\n", ErrBareLF, strings.Index("POST / HTTP/1.1\nHost: x\r\n\r\n", "\n")},
		{"stray_cr", "POST / HTTP/1.1\r\nX: a\rb\r\n\r\n", ErrStrayCR, strings.Index("POST / HTTP/1.1\r\nX: a\rb\r\n\r\n", "\rb")},
		{"nul_in_header", "POST / HTTP/1.1\r\nX: a\x00b\r\n\r\n", ErrNULByte, strings.Index("POST / HTTP/1.1\r\nX: a\x00b\r\n\r\n", "\x00")},
		{"bad_header_name", "POST / HTTP/1.1\r\nBad Name: v\r\n\r\n", ErrBadHeader, strings.Index("POST / HTTP/1.1\r\nBad Name: v\r\n\r\n", "Bad Name:")},
		{"missing_colon", "POST / HTTP/1.1\r\nBadHeader\r\n\r\n", ErrBadHeader, strings.Index("POST / HTTP/1.1\r\nBadHeader\r\n\r\n", "BadHeader")},
		{"bad_request_line", "POST  / HTTP/1.1\r\nHost: x\r\n\r\n", ErrBadRequestLine, 0},
		{"http10", "GET / HTTP/1.0\r\nHost: x\r\n\r\n", ErrBadRequestLine, 0},
		{"bad_te", "POST / HTTP/1.1\r\nTransfer-Encoding: gzip\r\n\r\n", ErrBadTransferEnc, -1},
		{"double_te", "POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\nTransfer-Encoding: chunked\r\n\r\n",
			ErrBadTransferEnc, -1},
		{"cl_overflow", "POST / HTTP/1.1\r\nContent-Length: 999999999999999999999999\r\n\r\n", ErrBadContentLength, -1},
		{"body_too_large", "POST / HTTP/1.1\r\nContent-Length: 100\r\n\r\n" + strings.Repeat("a", 100), ErrBodyTooLarge, -1},
		{"truncated_cl_body", "POST / HTTP/1.1\r\nContent-Length: 5\r\n\r\nabc", ErrTruncated, len("POST / HTTP/1.1\r\nContent-Length: 5\r\n\r\nabc")},
		{"truncated_headers", "POST / HTTP/1.1\r\nHost: x\r\n", ErrTruncated, len("POST / HTTP/1.1\r\nHost: x\r\n")},
		{"truncated_chunk_size", "POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhe", ErrTruncated, len("POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhe")},
		{"truncated_chunk_crlf", "POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello", ErrTruncated, len("POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello")},
		{"bad_chunk_digit", "POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n5z\r\nhello\r\n", ErrBadChunkSize, -1},
		{"chunk_size_overflow", "POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\nfffffffffffffffff\r\n", ErrBadChunkSize, -1},
		{"bad_chunk_terminator", "POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhelloXX", ErrBadChunkTerm, -1},
		{"bad_chunk_ext", "POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n5;a b\r\nhello\r\n", ErrBadChunkExt, -1},
		{"cl_in_trailer", "POST / HTTP/1.1\r\nTE: trailers\r\nTransfer-Encoding: chunked\r\n\r\n0\r\nContent-Length: 0\r\n\r\n", ErrBadTrailer, -1},
		{"empty", "", ErrTruncated, 0},
	}
}

func TestErrorOffsets(t *testing.T) {
	for _, tc := range errCases() {
		t.Run(tc.name, func(t *testing.T) {
			_, e := Parse([]byte(tc.raw), WithMaxBody(50))
			if e == nil {
				t.Fatalf("want error %s, got nil", tc.code)
			}
			if e.Code != tc.code {
				t.Fatalf("code=%s want %s (offset %d)", e.Code, tc.code, e.Offset)
			}
			if tc.offset >= 0 && e.Offset != tc.offset {
				t.Fatalf("offset=%d want %d (%s)", e.Offset, tc.offset, showAround(tc.raw, tc.offset))
			}
		})
	}
}

func showAround(s string, i int) string {
	lo, hi := i-8, i+8
	if lo < 0 {
		lo = 0
	}
	if hi > len(s) {
		hi = len(s)
	}
	return fmt.Sprintf("around %q", s[lo:hi])
}

// Acceptance: sweep every byte cut over a battery of streams and require
// identical framing results regardless of segmentation.
func TestSplitConsistencyAcceptance(t *testing.T) {
	streams := [][]byte{
		[]byte("GET / HTTP/1.1\r\nHost: x\r\n\r\n"),
		[]byte("POST /a HTTP/1.1\r\nContent-Length: 11\r\n\r\nhello world"),
		[]byte("POST /c HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"),
		[]byte("POST /c HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n3;a=b\r\nfoo\r\n0\r\nX: y\r\n\r\n"),
		// pipelined
		[]byte("GET /1 HTTP/1.1\r\nHost: a\r\n\r\nPOST /2 HTTP/1.1\r\nContent-Length: 3\r\n\r\nabc"),
		// ambiguous / invalid
		[]byte("POST / HTTP/1.1\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\nx\r\n0\r\n\r\n"),
		[]byte("POST / HTTP/1.1\r\nContent-Length: 5\r\n\r\nab"), // truncated
		[]byte("POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhe"),
		[]byte("POST / HTTP/1.1\r\nContent-Length: 2\r\n\tX: y\r\n\r\nab"),
		[]byte("POST / HTTP/1.1\r\nContent-Length: 5\r\nContent-Length: 6\r\n\r\nhello"),
		[]byte("POST / HTTP/1.1\r\nX: a\nb\r\n\r\n"),
	}
	for i, s := range streams {
		rep := VerifySplits(s)
		if !rep.OK {
			t.Fatalf("stream %d inconsistent: %+v", i, rep.Failures[:min(3, len(rep.Failures))])
		}
		if rep.CutsChecked != len(s)+1 {
			t.Fatalf("stream %d checked %d cuts", i, rep.CutsChecked)
		}
	}
}

// Random multi-way segmentation must also be stable.
func TestRandomSegmentation(t *testing.T) {
	raw := []byte("POST /c HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n")
	ref, rerr := runOneShot(raw, nil)
	// deterministic pseudo-random cuts
	cuts := []int{1, 1, 4, 2, 10, 3, 7, 1, 1, 5, 9, 2, 2, 1, 6, 8, 3, 1, 4, 2, 7, 11}
	d := NewDecoder()
	pos, got := 0, 0
	for pos < len(raw) {
		n := cuts[got%len(cuts)]
		if pos+n > len(raw) {
			n = len(raw) - pos
		}
		done, e := d.Write(raw[pos : pos+n])
		got += len(done)
		if e != nil {
			if rerr == nil || e.Code != rerr.Code || e.Offset != rerr.Offset {
				t.Fatalf("write error %v want %v", e, rerr)
			}
		}
		pos += n
	}
	if _, e := d.Close(); e != nil {
		t.Fatal(e)
	}
	if got != len(ref) {
		t.Fatalf("messages %d want %d", got, len(ref))
	}
	if string(d.msgs[0].Body) != string(ref[0].Body) {
		t.Fatalf("body mismatch")
	}
}
