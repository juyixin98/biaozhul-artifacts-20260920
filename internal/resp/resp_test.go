package resp

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"math/rand"
	"reflect"
	"strings"
	"testing"
)

// ---------------------------------------------------------------------------
// Independent reference encoder
//
// encodeRef is a deliberately second, independent implementation: instead of
// going through Writer it builds frames with explicit fmt.Sprintf calls and a
// hand-rolled recursion. Round-trip tests cross-check that Writer output and
// reference output are byte-identical, and that the parser accepts both.
// ---------------------------------------------------------------------------

func encodeRef(v *Value) []byte {
	var b bytes.Buffer
	switch v.Type {
	case SimpleString:
		fmt.Fprintf(&b, "+%s\r\n", v.Text)
	case Error:
		fmt.Fprintf(&b, "-%s\r\n", v.Text)
	case Integer:
		fmt.Fprintf(&b, ":%d\r\n", v.Int)
	case BulkString:
		fmt.Fprintf(&b, "$%d\r\n", len(v.Str))
		b.Write(v.Str)
		b.WriteString("\r\n")
	case NullBulk:
		b.WriteString("$-1\r\n")
	case NullArray:
		b.WriteString("*-1\r\n")
	case Array:
		fmt.Fprintf(&b, "*%d\r\n", len(v.Array))
		for _, c := range v.Array {
			b.Write(encodeRef(c))
		}
	}
	return b.Bytes()
}

func valuesEqual(a, b *Value) bool {
	if a == nil || b == nil {
		return a == b
	}
	if a.Type != b.Type {
		return false
	}
	switch a.Type {
	case SimpleString, Error:
		return a.Text == b.Text
	case Integer:
		return a.Int == b.Int
	case BulkString:
		return bytes.Equal(a.Str, b.Str)
	case NullBulk, NullArray:
		return true
	case Array:
		if len(a.Array) != len(b.Array) {
			return false
		}
		for i := range a.Array {
			if !valuesEqual(a.Array[i], b.Array[i]) {
				return false
			}
		}
		return true
	}
	return false
}

// sampleValues covers every type, nesting, and binary payload corner.
func sampleValues() []*Value {
	bin := []byte{0x00, 0x01, '\r', '\n', 0xff, 'a', 0x00, '\r'}
	return []*Value{
		{Type: SimpleString, Text: "OK"},
		{Type: Error, Text: "ERR something broke"},
		{Type: Integer, Int: 0},
		{Type: Integer, Int: -1},
		{Type: Integer, Int: 1 << 40},
		{Type: BulkString, Str: []byte("")}, // empty string, NOT null
		{Type: BulkString, Str: bin},        // binary safe incl CR LF NUL
		{Type: NullBulk},                    // null: $-1
		{Type: NullArray},                   // null: *-1
		{Type: Array, Array: []*Value{}},    // empty array, NOT null
		{Type: Array, Array: []*Value{ // GET-ish reply vector
			{Type: BulkString, Str: []byte("k")},
			{Type: NullBulk},
			{Type: BulkString, Str: []byte("")},
		}},
		nest(7, &Value{Type: BulkString, Str: []byte("deep")}),
	}
}

func nest(depth int, leaf *Value) *Value {
	v := leaf
	for i := 1; i < depth; i++ {
		v = &Value{Type: Array, Array: []*Value{v}}
	}
	return &Value{Type: Array, Array: []*Value{v}}
}

func TestWriterMatchesReferenceEncoder(t *testing.T) {
	for i, v := range sampleValues() {
		var got bytes.Buffer
		if err := NewWriter(&got).WriteValue(v); err != nil {
			t.Fatalf("case %d: writer error: %v", i, err)
		}
		want := encodeRef(v)
		if !bytes.Equal(got.Bytes(), want) {
			t.Fatalf("case %d (%v):\n writer=%q\n ref  =%q", i, v.Type, got.Bytes(), want)
		}
	}
}

func TestRoundTripBulk(t *testing.T) {
	// Decode bytes produced by Writer AND by the reference encoder; both
	// must yield the original Value, and re-encoding must be stable.
	for i, v := range sampleValues() {
		for _, frame := range [][]byte{encode(t, v), encodeRef(v)} {
			got, err := NewReader(bytes.NewReader(frame)).ReadMessage()
			if err != nil {
				t.Fatalf("case %d: parse %q: %v", i, frame, err)
			}
			if !valuesEqual(got, v) {
				t.Fatalf("case %d: parsed %+v, want %+v", i, got, v)
			}
			var again bytes.Buffer
			if err := NewWriter(&again).WriteValue(got); err != nil {
				t.Fatal(err)
			}
			if !bytes.Equal(again.Bytes(), frame) {
				t.Fatalf("case %d: re-encode mismatch\n got %q\nwant %q", i, again.Bytes(), frame)
			}
		}
	}
}

func encode(t *testing.T, v *Value) []byte {
	t.Helper()
	var b bytes.Buffer
	if err := NewWriter(&b).WriteValue(v); err != nil {
		t.Fatal(err)
	}
	return b.Bytes()
}

func TestPipelineMultipleMessages(t *testing.T) {
	// Three commands back to back: parser must find each frame boundary.
	cmds := []*Value{
		{Type: Array, Array: []*Value{{Type: BulkString, Str: []byte("PING")}}},
		{Type: Array, Array: []*Value{
			{Type: BulkString, Str: []byte("SET")},
			{Type: BulkString, Str: []byte("k")},
			{Type: BulkString, Str: []byte("v\r\n")}, // CRLF inside payload
		}},
		{Type: Array, Array: []*Value{{Type: BulkString, Str: []byte("GET")}, {Type: BulkString, Str: []byte("k")}}},
	}
	var stream bytes.Buffer
	for _, c := range cmds {
		stream.Write(encodeRef(c))
	}
	r := NewReader(&stream)
	for i, want := range cmds {
		got, err := r.ReadMessage()
		if err != nil {
			t.Fatalf("message %d: %v", i, err)
		}
		if !valuesEqual(got, want) {
			t.Fatalf("message %d mismatch", i)
		}
	}
	if _, err := r.ReadMessage(); !errors.Is(err, io.EOF) {
		t.Fatalf("expected clean EOF, got %v", err)
	}
}

// ---------------------------------------------------------------------------
// Single-byte delivery
//
// oneByteReader answers every Read with exactly one byte (or EOF). The codec
// must not depend on any read buffering/coalescing in the caller.
// ---------------------------------------------------------------------------

type oneByteReader struct{ data []byte }

func (r *oneByteReader) Read(p []byte) (int, error) {
	if len(r.data) == 0 {
		return 0, io.EOF
	}
	if len(p) == 0 {
		return 0, nil
	}
	p[0] = r.data[0]
	r.data = r.data[1:]
	return 1, nil
}

func TestSingleByteDelivery(t *testing.T) {
	rng := rand.New(rand.NewSource(42))
	values := append(sampleValues(), randomValue(rng, 4))
	var stream bytes.Buffer
	for _, v := range values {
		stream.Write(encodeRef(v))
	}
	r := NewReader(&oneByteReader{data: append([]byte(nil), stream.Bytes()...)})
	for i, want := range values {
		got, err := r.ReadMessage()
		if err != nil {
			t.Fatalf("single-byte message %d: %v", i, err)
		}
		if !valuesEqual(got, want) {
			t.Fatalf("single-byte message %d mismatch:\n got %+v\nwant %+v", i, got, want)
		}
	}
	if _, err := r.ReadMessage(); !errors.Is(err, io.EOF) {
		t.Fatalf("EOF: got %v", err)
	}
}

func randomValue(rng *rand.Rand, depth int) *Value {
	if depth <= 0 {
		switch rng.Intn(4) {
		case 0:
			return &Value{Type: Integer, Int: rng.Int63n(10000) - 5000}
		case 1:
			return &Value{Type: NullBulk}
		case 2:
			b := make([]byte, rng.Intn(32))
			rng.Read(b)
			return &Value{Type: BulkString, Str: b} // may be empty, may contain CRLF/NUL
		default:
			return &Value{Type: SimpleString, Text: "x"}
		}
	}
	n := rng.Intn(4)
	arr := make([]*Value, n)
	for i := range arr {
		arr[i] = randomValue(rng, depth-1)
	}
	return &Value{Type: Array, Array: arr}
}

// ---------------------------------------------------------------------------
// Half packets: EOF in the middle of a frame is ErrUnexpectedEOF, never a
// successful parse and never a bare io.EOF.
// ---------------------------------------------------------------------------

func TestHalfPackets(t *testing.T) {
	full := "*2\r\n$3\r\nfoo\r\n$3\r\nbar\r\n"
	cuts := []int{
		1,             // "*"
		3,             // "*2\r"
		4,             // "*2\r\n"
		6,             // "*2\r\n$3"
		9,             // inside first bulk payload
		12,            // after first complete element
		len(full) - 1, // missing final \n
	}
	for _, cut := range cuts {
		r := NewReader(bytes.NewReader([]byte(full[:cut])))
		v, err := r.ReadMessage()
		if !errors.Is(err, ErrUnexpectedEOF) {
			t.Fatalf("cut=%d: want ErrUnexpectedEOF, got v=%v err=%v", cut, v, err)
		}
	}

	// Clean end-of-stream between frames is still plain io.EOF.
	r := NewReader(bytes.NewReader(nil))
	if _, err := r.ReadMessage(); !errors.Is(err, io.EOF) {
		t.Fatalf("empty stream: want io.EOF, got %v", err)
	}
}

// A half packet may follow complete pipelined messages: earlier replies are
// unaffected, only the trailing frame errors.
func TestHalfPacketAfterCompleteMessages(t *testing.T) {
	good := encodeRef(&Value{Type: Array, Array: []*Value{{Type: BulkString, Str: []byte("PING")}}})
	stream := append(append([]byte{}, good...), []byte("*2\r\n$1\r\na\r\n$5\r\nxyz")...)
	r := NewReader(bytes.NewReader(stream))
	if _, err := r.ReadMessage(); err != nil {
		t.Fatal(err)
	}
	if _, err := r.ReadMessage(); !errors.Is(err, ErrUnexpectedEOF) {
		t.Fatalf("want ErrUnexpectedEOF, got %v", err)
	}
}

// ---------------------------------------------------------------------------
// Malformed frames: negative lengths, bad integers, bad line endings,
// over-long declarations, nesting depth.
// ---------------------------------------------------------------------------

func TestMalformedFrames(t *testing.T) {
	bad := []string{
		"$-2\r\n",                   // negative length other than -1
		"*-2\r\n",                   // negative array count other than -1
		"$5\r\nabcdeX\n",            // payload trailer is X\n instead of \r\n
		":01\r\n",                   // leading zero
		":-0\r\n",                   // negative zero
		":+1\r\n",                   // plus sign
		":1x\r\n",                   // garbage digit
		"abc\r\n",                   // unknown type byte
		"$3\r\nab\r\n",              // declared 3, payload + CRLF cut by next byte (bad trailer)
		"ping\n",                    // bare LF, no CR
		"$99999999999999999999\r\n", // declared length overflows int64
		"*1\r\n",                    // array declares 1 element, stream ends
	}
	for i, frame := range bad {
		r := NewReader(bytes.NewReader([]byte(frame)))
		v, err := r.ReadMessage()
		if !errors.Is(err, ErrProtocol) && !errors.Is(err, ErrUnexpectedEOF) {
			t.Fatalf("case %d %q: want protocol/EOF error, got v=%v err=%v", i, frame, v, err)
		}
	}
}

func TestBulkLengthCap(t *testing.T) {
	// One byte over the cap: rejected without allocating/reading the body.
	frame := fmt.Sprintf("$%d\r\n", MaxBulkLength+1)
	_, err := NewReader(strings.NewReader(frame)).ReadMessage()
	if !errors.Is(err, ErrProtocol) || !strings.Contains(err.Error(), "exceeds limit") {
		t.Fatalf("want length-limit protocol error, got %v", err)
	}

	// Exactly the cap parses (allocation is large but legitimate).
	payload := make([]byte, MaxBulkLength)
	frame = fmt.Sprintf("$%d\r\n", MaxBulkLength) + string(payload) + "\r\n"
	v, err := NewReader(strings.NewReader(frame)).ReadMessage()
	if err != nil {
		t.Fatalf("max-length bulk: %v", err)
	}
	if len(v.Str) != MaxBulkLength {
		t.Fatalf("got %d bytes", len(v.Str))
	}
}

func TestArrayCountCap(t *testing.T) {
	frame := fmt.Sprintf("*%d\r\n", MaxArrayElements+1)
	_, err := NewReader(strings.NewReader(frame)).ReadMessage()
	if !errors.Is(err, ErrProtocol) || !strings.Contains(err.Error(), "exceeds limit") {
		t.Fatalf("got %v", err)
	}
}

func TestNestingDepthCap(t *testing.T) {
	ok := nest(MaxNestingDepth, &Value{Type: BulkString, Str: []byte("x")})
	if _, err := NewReader(bytes.NewReader(encodeRef(ok))).ReadMessage(); err != nil {
		t.Fatalf("depth %d should be allowed: %v", MaxNestingDepth, err)
	}
	tooDeep := nest(MaxNestingDepth+1, &Value{Type: BulkString, Str: []byte("x")})
	_, err := NewReader(bytes.NewReader(encodeRef(tooDeep))).ReadMessage()
	if !errors.Is(err, ErrProtocol) || !strings.Contains(err.Error(), "nesting") {
		t.Fatalf("want nesting protocol error, got %v", err)
	}
}

func TestNullIsNotEmptyString(t *testing.T) {
	// The single most important semantic distinction in RESP2.
	r := NewReader(strings.NewReader("$-1\r\n$0\r\n\r\n"))
	null, err := r.ReadMessage()
	if err != nil {
		t.Fatal(err)
	}
	empty, err := r.ReadMessage()
	if err != nil {
		t.Fatal(err)
	}
	if null.Type != NullBulk || empty.Type != BulkString || len(empty.Str) != 0 {
		t.Fatalf("null=%v empty(type=%d,len=%d)", null.Type, empty.Type, len(empty.Str))
	}
	if reflect.DeepEqual(null, empty) {
		t.Fatal("null bulk and empty bulk must be different values")
	}
}

func FuzzCodecRoundTrip(f *testing.F) {
	seeds := [][]byte{
		[]byte("+OK\r\n"),
		[]byte(":42\r\n"),
		[]byte("$-1\r\n"),
		[]byte("$0\r\n\r\n"),
		[]byte("*0\r\n"),
		[]byte("*-1\r\n"),
		[]byte("*2\r\n$1\r\na\r\n$1\r\nb\r\n"),
		[]byte("$4\r\n\r\n\r\n\r\n"), // payload is CR CR LF
	}
	for _, s := range seeds {
		f.Add(s)
	}
	f.Fuzz(func(t *testing.T, raw []byte) {
		v, err := NewReader(bytes.NewReader(raw)).ReadMessage()
		if err != nil {
			return // random garbage is allowed to fail
		}
		// Whatever parsed must re-encode to exactly the canonical bytes.
		var b bytes.Buffer
		if err := NewWriter(&b).WriteValue(v); err != nil {
			t.Fatal(err)
		}
		v2, err := NewReader(bytes.NewReader(b.Bytes())).ReadMessage()
		if err != nil {
			t.Fatalf("canonical re-encode does not parse: %v\n%s", err, b.Bytes())
		}
		if !valuesEqual(v, v2) {
			t.Fatal("value changed across canonical round trip")
		}
	})
}
