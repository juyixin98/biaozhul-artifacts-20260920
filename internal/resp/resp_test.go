package resp_test

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"math"
	"strings"
	"testing"

	"respd/internal/resp"
)

// ---------------------------------------------------------------------------
// independentEncoder is a deliberately different, reference-style RESP2
// writer built by string concatenation rather than sharing any code with
// the production Encoder. The acceptance suite cross-checks production
// output against it byte for byte.
// ---------------------------------------------------------------------------

type independentEncoder struct{ buf bytes.Buffer }

func (e *independentEncoder) put(v resp.Value) {
	switch v.Kind {
	case resp.KindSimple:
		fmt.Fprintf(&e.buf, "+%s\r\n", e.safe(v.Str))
	case resp.KindError:
		fmt.Fprintf(&e.buf, "-%s\r\n", e.safe(v.Str))
	case resp.KindInteger:
		fmt.Fprintf(&e.buf, ":%d\r\n", v.N)
	case resp.KindBulk:
		if v.Bulk == nil {
			e.buf.WriteString("$-1\r\n")
		} else {
			fmt.Fprintf(&e.buf, "$%d\r\n", len(v.Bulk))
			e.buf.Write(v.Bulk)
			e.buf.WriteString("\r\n")
		}
	case resp.KindArray:
		if v.Array == nil {
			e.buf.WriteString("*-1\r\n")
		} else {
			fmt.Fprintf(&e.buf, "*%d\r\n", len(v.Array))
			for _, c := range v.Array {
				e.put(c)
			}
		}
	default:
		panic("bad kind")
	}
}

func (e *independentEncoder) safe(s string) string {
	return strings.NewReplacer("\r", " ", "\n", " ").Replace(s)
}

func (e *independentEncoder) bytes() []byte { return e.buf.Bytes() }

// oneByteReader delivers its payload one byte per Read, no matter how large
// a buffer the caller offers. It is the strictest possible streaming test.
type oneByteReader struct {
	data []byte
	pos  int
}

func (r *oneByteReader) Read(p []byte) (int, error) {
	if r.pos >= len(r.data) {
		return 0, io.EOF
	}
	if len(p) == 0 {
		return 0, nil
	}
	p[0] = r.data[r.pos]
	r.pos++
	return 1, nil
}

// slowReader splits every frame at every possible offset: Read returns up
// to n bytes where n cycles 1,2,3...
type slowReader struct {
	data []byte
	pos  int
	n    int
}

func (r *slowReader) Read(p []byte) (int, error) {
	if r.pos >= len(r.data) {
		return 0, io.EOF
	}
	r.n++
	max := r.n % 5
	if max == 0 {
		max = 5
	}
	if max > len(p) {
		max = len(p)
	}
	if max > len(r.data)-r.pos {
		max = len(r.data) - r.pos
	}
	copy(p, r.data[r.pos:r.pos+max])
	r.pos += max
	return max, nil
}

func sampleValues() []resp.Value {
	nested := resp.ArrayValue(
		resp.ArrayValue(resp.BulkString("a"), resp.Integer(1)),
		resp.ArrayValue(resp.BulkString("b"), resp.Integer(2),
			resp.ArrayValue(resp.NullBulk(), resp.SimpleString("x"))),
	)
	return []resp.Value{
		resp.SimpleString("OK"),
		resp.SimpleError("ERR something broke"),
		resp.Integer(0),
		resp.Integer(math.MaxInt64),
		resp.Integer(math.MinInt64),
		resp.BulkString(""),                              // empty string, NOT null
		resp.BulkString("hello"),                         // normal bulk
		resp.BulkBytes([]byte{0, 1, 2, 255, '\r', '\n'}), // binary safe incl. CRLF
		resp.NullBulk(),                                  // $-1
		resp.ArrayValue(),                                // *0
		resp.NullArray(),                                 // *-1
		resp.ArrayValue(resp.BulkString("PING")),
		resp.ArrayValue(resp.BulkString("SET"), resp.BulkString("k"),
			resp.NullBulk()),
		nested,
	}
}

func TestEncoderMatchesIndependentReference(t *testing.T) {
	vals := sampleValues()
	for i, v := range vals {
		var prod bytes.Buffer
		enc := resp.NewEncoder(&prod)
		if err := enc.Encode(v); err != nil {
			t.Fatalf("case %d: production encode: %v", i, err)
		}
		ref := independentEncoder{}
		ref.put(v)
		if !bytes.Equal(prod.Bytes(), ref.bytes()) {
			t.Fatalf("case %d (%v):\nprod %q\nref  %q", i, v.Kind, prod.Bytes(), ref.bytes())
		}
	}
}

func TestDecodeRoundTripOneByteAtATime(t *testing.T) {
	vals := sampleValues()
	var wire bytes.Buffer
	ref := independentEncoder{}
	for _, v := range vals {
		ref.put(v)
		wire.Write(ref.bytes())
		ref.buf.Reset()
	}
	dec := resp.NewDecoder(&oneByteReader{data: wire.Bytes()})
	for i, want := range vals {
		got, err := dec.Next()
		if err != nil {
			t.Fatalf("case %d: decode over one-byte stream: %v", i, err)
		}
		if !valuesEqual(got, want) {
			t.Fatalf("case %d:\ngot  %+v\nwant %+v", i, got, want)
		}
	}
	if _, err := dec.Next(); err != io.EOF {
		t.Fatalf("want clean EOF, got %v", err)
	}
}

func TestDecodeRoundTripSlowReader(t *testing.T) {
	// Whole pipeline of every sample, read with varying tiny chunk sizes.
	vals := sampleValues()
	ref := independentEncoder{}
	for _, v := range vals {
		ref.put(v)
	}
	dec := resp.NewDecoder(&slowReader{data: ref.bytes()})
	for i, want := range vals {
		got, err := dec.Next()
		if err != nil {
			t.Fatalf("case %d: %v", i, err)
		}
		if !valuesEqual(got, want) {
			t.Fatalf("case %d mismatch: got %+v want %+v", i, got, want)
		}
	}
}

func valuesEqual(a, b resp.Value) bool {
	if a.Kind != b.Kind {
		return false
	}
	switch a.Kind {
	case resp.KindSimple, resp.KindError:
		return a.Str == b.Str
	case resp.KindInteger:
		return a.N == b.N
	case resp.KindBulk:
		if (a.Bulk == nil) != (b.Bulk == nil) {
			return false
		}
		return bytes.Equal(a.Bulk, b.Bulk)
	case resp.KindArray:
		if (a.Array == nil) != (b.Array == nil) {
			return false
		}
		if len(a.Array) != len(b.Array) {
			return false
		}
		for i := range a.Array {
			if !valuesEqual(a.Array[i], b.Array[i]) {
				return false
			}
		}
	}
	return true
}

// ---------------------------------------------------------------------------
// Negative / malformed length handling.
// ---------------------------------------------------------------------------

func TestNegativeLengths(t *testing.T) {
	cases := map[string]string{
		"bulk -2 (only -1 is null)":    "$-2\r\n",
		"bulk -100":                    "$-100\r\n",
		"array -2 (only -1 is null)":   "*-2\r\n",
		"array -5":                     "*-5\r\n",
		"bulk length with plus sign":   "$+5\r\nhello\r\n",
		"bulk length with letters":     "$abc\r\n",
		"array length with space":      "* 2\r\n",
		"integer with plus":            ":+1\r\n",
		"integer with letters":         ":xyz\r\n",
		"lone minus in integer":        ":-\r\n",
		"bulk missing CRLF terminator": "$5\r\nhelloXX",
		"array element bad type byte":  "*2\r\n$1\r\na\r\n~1\r\nx\r\n",
		"bulk bigger than declared":    "$5\r\nhelloXX\r\n",
		"bare LF, no CR":               "$5\nhello\n",
	}
	for name, input := range cases {
		t.Run(name, func(t *testing.T) {
			dec := resp.NewDecoder(strings.NewReader(input))
			_, err := dec.Next()
			if err == nil {
				t.Fatalf("expected protocol error for %q, got nil", input)
			}
			if !errors.Is(err, resp.ErrProtocol) {
				t.Fatalf("expected ErrProtocol, got %T: %v", err, err)
			}
		})
	}
}

func TestDeclaredLengthLimits(t *testing.T) {
	limits := resp.Limits{
		MaxLineLen:   64,
		MaxBulkLen:   4,
		MaxArrayLen:  3,
		MaxNesting:   2,
		MaxInlineLen: 64,
	}
	t.Run("bulk over cap rejected before reading payload", func(t *testing.T) {
		dec := resp.NewDecoderLimits(strings.NewReader("$5\r\nhello\r\n"), limits)
		_, err := dec.Next()
		if err == nil || !errors.Is(err, resp.ErrProtocol) {
			t.Fatalf("want protocol error, got %v", err)
		}
	})
	t.Run("array over cap rejected", func(t *testing.T) {
		dec := resp.NewDecoderLimits(strings.NewReader("*4\r\n:1\r\n:2\r\n:3\r\n:4\r\n"), limits)
		_, err := dec.Next()
		if err == nil || !errors.Is(err, resp.ErrProtocol) {
			t.Fatalf("want protocol error, got %v", err)
		}
	})
	t.Run("overlong line rejected", func(t *testing.T) {
		dec := resp.NewDecoderLimits(strings.NewReader("+"+strings.Repeat("x", 65)+"\r\n"), limits)
		_, err := dec.Next()
		if err == nil || !errors.Is(err, resp.ErrProtocol) {
			t.Fatalf("want protocol error, got %v", err)
		}
	})
}

func TestNestingDepth(t *testing.T) {
	limits := resp.Limits{
		MaxLineLen:   1024,
		MaxBulkLen:   1024,
		MaxArrayLen:  1024,
		MaxNesting:   7,
		MaxInlineLen: 1024,
	}
	mk := func(depth int) string {
		var b strings.Builder
		for i := 0; i < depth; i++ {
			b.WriteString("*1\r\n")
		}
		b.WriteString(":7\r\n")
		return b.String()
	}
	// Depth 7 accepted.
	dec := resp.NewDecoderLimits(strings.NewReader(mk(7)), limits)
	if _, err := dec.Next(); err != nil {
		t.Fatalf("depth 7 should pass: %v", err)
	}
	// Depth 8 rejected.
	dec = resp.NewDecoderLimits(strings.NewReader(mk(8)), limits)
	_, err8 := dec.Next()
	if err8 == nil || !errors.Is(err8, resp.ErrProtocol) {
		t.Fatalf("depth 8 should fail with protocol error, got %v", err8)
	}
}

// ---------------------------------------------------------------------------
// Half packets: EOF in the middle of every frame position is a truncated
// protocol error, while a clean EOF between frames is plain io.EOF.
// ---------------------------------------------------------------------------

func TestTruncatedFrames(t *testing.T) {
	full := []string{
		"$5\r\nhello\r\n",
		"*2\r\n$1\r\na\r\n$1\r\nb\r\n",
		":1234\r\n",
		"+OK\r\n",
	}
	for _, f := range full {
		for cut := 1; cut < len(f); cut++ {
			dec := resp.NewDecoder(strings.NewReader(f[:cut]))
			_, err := dec.Next()
			pe := resp.AsProtocolError(err)
			if pe == nil || !pe.Truncated() {
				t.Fatalf("cut %d/%d of %q: want truncated ProtocolError, got %v",
					cut, len(f), f, err)
			}
		}
	}
}

func TestCleanEOFBetweenFrames(t *testing.T) {
	dec := resp.NewDecoder(strings.NewReader(":1\r\n:2\r\n"))
	if v, err := dec.Next(); err != nil || v.N != 1 {
		t.Fatalf("first: %v %v", v, err)
	}
	if v, err := dec.Next(); err != nil || v.N != 2 {
		t.Fatalf("second: %v %v", v, err)
	}
	if _, err := dec.Next(); err != io.EOF {
		t.Fatalf("want io.EOF, got %v", err)
	}
}

func TestEmptyStringIsNotNull(t *testing.T) {
	dec := resp.NewDecoder(strings.NewReader("$0\r\n\r\n$-1\r\n"))
	empty, err := dec.Next()
	if err != nil {
		t.Fatal(err)
	}
	if empty.Kind != resp.KindBulk || empty.Bulk == nil || len(empty.Bulk) != 0 {
		t.Fatalf("want non-nil empty bulk, got %+v", empty)
	}
	null, err := dec.Next()
	if err != nil {
		t.Fatal(err)
	}
	if !null.IsNull() || null.Bulk != nil {
		t.Fatalf("want null bulk, got %+v", null)
	}
}

func TestInlineCommands(t *testing.T) {
	in := "PING\r\nSET  key value  with spaces \r\n"
	dec := resp.NewDecoder(strings.NewReader(in))
	v, err := dec.Next()
	if err != nil {
		t.Fatal(err)
	}
	if len(v.Array) != 1 || string(v.Array[0].Bulk) != "PING" {
		t.Fatalf("inline PING: %+v", v)
	}
	v, err = dec.Next()
	if err != nil {
		t.Fatal(err)
	}
	got := make([]string, len(v.Array))
	for i, e := range v.Array {
		got[i] = string(e.Bulk)
	}
	want := []string{"SET", "key", "value", "with", "spaces"}
	if strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("inline tokens: got %v want %v", got, want)
	}
}

// TestPipelineManyCommands pumps a large pipeline byte by byte to make
// sure ordering and framing never drift.
func TestPipelineManyCommands(t *testing.T) {
	const n = 2000
	var wire bytes.Buffer
	ref := independentEncoder{}
	for i := 0; i < n; i++ {
		ref.put(resp.ArrayValue(
			resp.BulkString("SET"),
			resp.BulkString(fmt.Sprintf("k%d", i)),
			resp.BulkString(fmt.Sprintf("v%d", i)),
		))
		wire.Write(ref.bytes())
		ref.buf.Reset()
	}
	dec := resp.NewDecoder(&oneByteReader{data: wire.Bytes()})
	for i := 0; i < n; i++ {
		v, err := dec.Next()
		if err != nil {
			t.Fatalf("command %d: %v", i, err)
		}
		if len(v.Array) != 3 ||
			string(v.Array[1].Bulk) != fmt.Sprintf("k%d", i) ||
			string(v.Array[2].Bulk) != fmt.Sprintf("v%d", i) {
			t.Fatalf("command %d mismatch: %+v", i, v)
		}
	}
}
