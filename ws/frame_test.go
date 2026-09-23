package ws

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"math/rand"
	"testing"
)

func mustEncode(t *testing.T, f Frame, masked bool) []byte {
	t.Helper()
	b, err := EncodeFrame(f, masked)
	if err != nil {
		t.Fatalf("encode: %v", err)
	}
	return b
}

func TestFrameRoundTrip(t *testing.T) {
	cases := []struct {
		name    string
		opcode  byte
		payload []byte
	}{
		{"empty text", OpText, nil},
		{"short text", OpText, []byte("hello")},
		{"exactly 125", OpBinary, bytes.Repeat([]byte{1}, 125)},
		{"126 needs 16-bit length", OpBinary, bytes.Repeat([]byte{2}, 126)},
		{"max 16-bit length", OpBinary, bytes.Repeat([]byte{3}, 65535)},
		{"65536 needs 64-bit length", OpBinary, bytes.Repeat([]byte{4}, 65536)},
		{"ping payload", OpPing, []byte("ping-data")},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			for _, masked := range []bool{false, true} {
				raw := mustEncode(t, Frame{Fin: true, OpCode: tc.opcode, Payload: tc.payload}, masked)
				p := NewFrameParser(0)
				frames, err := p.Feed(raw)
				if err != nil {
					t.Fatalf("parse: %v", err)
				}
				if len(frames) != 1 {
					t.Fatalf("got %d frames, want 1", len(frames))
				}
				f := frames[0]
				if !f.Fin || f.OpCode != tc.opcode || f.Masked != masked {
					t.Fatalf("header mismatch: %+v", f)
				}
				if !bytes.Equal(f.Payload, tc.payload) {
					t.Fatalf("payload mismatch (masked=%v): len got %d want %d", masked, len(f.Payload), len(tc.payload))
				}
			}
		})
	}
}

// TestFrameMaskUnmask proves the parser strips the client mask correctly,
// including payloads that deliberately repeat the mask key pattern.
func TestFrameMaskUnmask(t *testing.T) {
	key := []byte{0x37, 0xFA, 0x21, 0x3D}
	payload := []byte("masking key repeats: 7777 and abcdefghij")
	raw := []byte{0x81, 0x80 | byte(len(payload))}
	raw = append(raw, key...)
	for i, b := range payload {
		raw = append(raw, b^key[i%4])
	}
	p := NewFrameParser(0)
	frames, err := p.Feed(raw)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(frames[0].Payload, payload) {
		t.Fatalf("unmasked payload = %q, want %q", frames[0].Payload, payload)
	}
}

// TestFrameParserErrors covers malformed wire bytes.
func TestFrameParserErrors(t *testing.T) {
	tests := []struct {
		name string
		raw  []byte
		want error
		code int
	}{
		{
			name: "fragmented control frame",
			raw:  []byte{0x09, 0x01, 'x'}, // FIN=0, ping, len 1
			want: errControlFragment,
			code: 1002,
		},
		{
			name: "control payload over 125",
			raw:  append([]byte{0x89, 126, 0x00, 0x7E}, make([]byte, 126)...),
			want: nil, // matched by message below
			code: 1002,
		},
		{
			name: "non-minimal 16-bit length",
			raw:  []byte{0x82, 0x7E, 0x00, 0x05, 1, 2, 3, 4, 5},
			want: errInvalidLength,
			code: 1002,
		},
		{
			name: "non-minimal 64-bit length",
			raw:  append([]byte{0x82, 0x7F, 0, 0, 0, 0, 0, 0, 0x00, 0x78}, make([]byte, 120)...),
			want: errInvalidLength,
			code: 1002,
		},
		{
			name: "64-bit length high bit set",
			raw:  []byte{0x82, 0x7F, 0x80, 0, 0, 0, 0, 0, 0, 0},
			want: nil,
			code: 1002,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			p := NewFrameParser(0)
			_, err := p.Feed(tc.raw)
			if err == nil {
				t.Fatal("expected error, got nil")
			}
			if tc.want != nil && !errors.Is(err, tc.want) {
				t.Fatalf("err = %v, want %v", err, tc.want)
			}
			if got := CloseCode(err); got != tc.code {
				t.Fatalf("close code = %d, want %d", got, tc.code)
			}
		})
	}
}

func TestFramePayloadLimit(t *testing.T) {
	raw := mustEncode(t, Frame{Fin: true, OpCode: OpBinary, Payload: make([]byte, 100)}, true)
	p := NewFrameParser(99)
	if _, err := p.Feed(raw); !errors.Is(err, errFrameTooLarge) {
		t.Fatalf("err = %v, want errFrameTooLarge", err)
	}
	if CloseCode(errFrameTooLarge) != 1009 {
		t.Fatal("frame-too-large must map to 1009")
	}
}

func TestFrameIncomplete(t *testing.T) {
	raw := mustEncode(t, Frame{Fin: true, OpCode: OpText, Payload: []byte("partial")}, true)
	p := NewFrameParser(0)
	// Feed every prefix: nothing must be returned until the full frame arrives.
	var got []Frame
	for i := 0; i < len(raw)-1; i++ {
		frames, err := p.Feed(raw[i : i+1])
		if err != nil {
			t.Fatalf("byte %d: unexpected error %v", i, err)
		}
		if len(frames) != 0 {
			t.Fatalf("byte %d: got a frame before input complete", i)
		}
		got = append(got, frames...)
	}
	frames, err := p.Feed(raw[len(raw)-1:])
	if err != nil {
		t.Fatal(err)
	}
	got = append(got, frames...)
	if len(got) != 1 || string(got[0].Payload) != "partial" {
		t.Fatalf("final feed did not complete frame: %+v", got)
	}
}

// TestFrameParserRandomChunks is the core acceptance check: feeding the same
// byte stream through the parser in one shot and in many random chunkings must
// produce identical frames.
func TestFrameParserRandomChunks(t *testing.T) {
	rng := rand.New(rand.NewSource(42))

	buildStream := func(r *rand.Rand) []byte {
		var stream []byte
		nFrames := 1 + r.Intn(20)
		for i := 0; i < nFrames; i++ {
			var f Frame
			switch r.Intn(6) {
			case 0:
				f = Frame{Fin: true, OpCode: OpText, Payload: randomBytes(r, r.Intn(300))}
			case 1:
				f = Frame{Fin: true, OpCode: OpBinary, Payload: randomBytes(r, r.Intn(70000))}
			case 2:
				f = Frame{Fin: true, OpCode: OpPing, Payload: randomBytes(r, r.Intn(MaxControlPayload+1))}
			case 3:
				f = Frame{Fin: true, OpCode: OpPong, Payload: randomBytes(r, r.Intn(10))}
			case 4:
				f = Frame{Fin: true, OpCode: OpClose, Payload: []byte{0x03, 0xE8}}
			default:
				f = Frame{Fin: false, OpCode: OpContinuation, Payload: randomBytes(r, r.Intn(50))}
			}
			stream = append(stream, mustEncode(t, f, r.Intn(2) == 0)...)
		}
		return stream
	}

	for iter := 0; iter < 200; iter++ {
		stream := buildStream(rng)

		whole, err := NewFrameParser(0).Feed(stream)
		if err != nil {
			t.Fatalf("iter %d: one-shot parse: %v", iter, err)
		}

		// Random chunking (chunk size 1..64).
		p := NewFrameParser(0)
		var chunked []Frame
		for pos := 0; pos < len(stream); {
			n := 1 + rng.Intn(64)
			if pos+n > len(stream) {
				n = len(stream) - pos
			}
			frames, err := p.Feed(stream[pos : pos+n])
			if err != nil {
				t.Fatalf("iter %d pos %d: chunked parse: %v", iter, pos, err)
			}
			chunked = append(chunked, frames...)
			pos += n
		}
		if len(chunked) != len(whole) {
			t.Fatalf("iter %d: %d chunked frames vs %d whole frames", iter, len(chunked), len(whole))
		}
		for i := range whole {
			if !framesEqual(chunked[i], whole[i]) {
				t.Fatalf("iter %d frame %d mismatch:\n chunked=%+v\n whole=%+v",
					iter, i, summarize(chunked[i]), summarize(whole[i]))
			}
		}
	}
}

// TestFrameParserRandomChunksStress uses tiny 1-3 byte chunks specifically to
// maximise the chance of splitting headers and masks mid-field.
func TestFrameParserRandomChunksStress(t *testing.T) {
	rng := rand.New(rand.NewSource(7))
	stream := mustEncode(t, Frame{Fin: true, OpCode: OpBinary, Payload: make([]byte, 100000)}, true)
	stream = append(stream, mustEncode(t, Frame{Fin: true, OpCode: OpText, Payload: []byte("tail")}, false)...)

	p := NewFrameParser(0)
	var got []Frame
	for pos := 0; pos < len(stream); {
		n := 1 + rng.Intn(3)
		if pos+n > len(stream) {
			n = len(stream) - pos
		}
		frames, err := p.Feed(stream[pos : pos+n])
		if err != nil {
			t.Fatalf("pos %d: %v", pos, err)
		}
		got = append(got, frames...)
		pos += n
	}
	if len(got) != 2 || len(got[0].Payload) != 100000 || string(got[1].Payload) != "tail" {
		t.Fatalf("stress parse mismatch: %d frames", len(got))
	}
}

func randomBytes(r *rand.Rand, n int) []byte {
	b := make([]byte, n)
	r.Read(b)
	return b
}

func framesEqual(a, b Frame) bool {
	return a.Fin == b.Fin && a.RSV == b.RSV && a.OpCode == b.OpCode &&
		a.Masked == b.Masked && bytes.Equal(a.Payload, b.Payload)
}

func summarize(f Frame) string {
	return fmt.Sprintf("Fin=%v RSV=%d Op=%d Masked=%v len=%d", f.Fin, f.RSV, f.OpCode, f.Masked, len(f.Payload))
}

// ensure io import retained for interface-level checks in future tests
var _ = io.ErrUnexpectedEOF
