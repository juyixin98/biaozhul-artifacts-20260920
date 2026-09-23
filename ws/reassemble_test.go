package ws

import (
	"errors"
	"math/rand"
	"testing"
)

func mustHandle(t *testing.T, r *Reassembler, f Frame) *Event {
	t.Helper()
	ev, err := r.HandleFrame(f)
	if err != nil {
		t.Fatalf("HandleFrame opcode=%d: %v (code %d)", f.OpCode, err, CloseCode(err))
	}
	return ev
}

func fin(op byte, p []byte) Frame { return Frame{Fin: true, OpCode: op, Payload: p, Masked: true} }

func frag(op byte, p []byte) Frame { return Frame{Fin: false, OpCode: op, Payload: p, Masked: true} }
func cont(p []byte, final bool) Frame {
	return Frame{Fin: final, OpCode: OpContinuation, Payload: p, Masked: true}
}

func TestReassembleUnfragmented(t *testing.T) {
	r := NewReassembler(ReassemblerConfig{RequireMask: true})
	ev := mustHandle(t, r, fin(OpText, []byte("abc")))
	if ev.Type != EventMessage || ev.OpCode != OpText || string(ev.Payload) != "abc" {
		t.Fatalf("bad event: %+v", ev)
	}
	ev = mustHandle(t, r, fin(OpBinary, []byte{1, 2, 3}))
	if ev.OpCode != OpBinary || len(ev.Payload) != 3 {
		t.Fatalf("bad binary event: %+v", ev)
	}
}

func TestReassembleFragmentedText(t *testing.T) {
	r := NewReassembler(ReassemblerConfig{})
	// Split "你好" (E4 BD A0 E5 A5 BD) across three fragments mid-codepoint.
	parts := [][]byte{{'a', 0xE4}, {0xBD, 0xA0, 0xE5}, {0xA5, 0xBD, 'z'}}
	if ev, err := r.HandleFrame(frag(OpText, parts[0])); err != nil || ev != nil {
		t.Fatalf("start: ev=%v err=%v", ev, err)
	}
	if ev, err := r.HandleFrame(cont(parts[1], false)); err != nil || ev != nil {
		t.Fatalf("mid: ev=%v err=%v", ev, err)
	}
	ev, err := r.HandleFrame(cont(parts[2], true))
	if err != nil {
		t.Fatalf("final: %v", err)
	}
	want := append(append(append([]byte{}, parts[0]...), parts[1]...), parts[2]...)
	if string(ev.Payload) != string(want) {
		t.Fatalf("payload = % x, want % x", ev.Payload, want)
	}
}

func TestControlFramesInterleave(t *testing.T) {
	r := NewReassembler(ReassemblerConfig{})
	if _, err := r.HandleFrame(frag(OpText, []byte("x"))); err != nil {
		t.Fatal(err)
	}
	ev := mustHandle(t, r, fin(OpPing, []byte("p")))
	if ev.Type != EventPing || string(ev.Payload) != "p" {
		t.Fatalf("ping event: %+v", ev)
	}
	ev = mustHandle(t, r, fin(OpPong, []byte("q")))
	if ev.Type != EventPong {
		t.Fatalf("pong event: %+v", ev)
	}
	// Another data frame while a message is open must be rejected.
	_, err := r.HandleFrame(frag(OpText, []byte("nested")))
	if !errors.Is(err, errMessageActive) {
		t.Fatalf("nested start err = %v, want errMessageActive", err)
	}
	// Completing the original message still works.
	ev = mustHandle(t, r, cont([]byte("y"), true))
	if string(ev.Payload) != "xy" {
		t.Fatalf("payload = %q, want xy", ev.Payload)
	}
}

func TestUnexpectedContinuation(t *testing.T) {
	r := NewReassembler(ReassemblerConfig{})
	_, err := r.HandleFrame(cont([]byte("x"), true))
	if !errors.Is(err, errNoMessage) || CloseCode(err) != 1002 {
		t.Fatalf("err = %v code %d", err, CloseCode(err))
	}
}

func TestInvalidUTF8(t *testing.T) {
	r := NewReassembler(ReassemblerConfig{})

	// Invalid bytes inside a continuation frame.
	_, err := r.HandleFrame(frag(OpText, []byte("ok:")))
	if err != nil {
		t.Fatal(err)
	}
	_, err = r.HandleFrame(cont([]byte{0xFF, 0xFE}, true))
	if !errors.Is(err, errUTF8) || CloseCode(err) != 1007 {
		t.Fatalf("invalid continuation bytes: err=%v code=%d", err, CloseCode(err))
	}

	// Dangling lead byte at the end of the message ("half a multibyte char").
	r = NewReassembler(ReassemblerConfig{})
	_, err = r.HandleFrame(frag(OpText, []byte("hi-")))
	if err != nil {
		t.Fatal(err)
	}
	_, err = r.HandleFrame(cont([]byte{0xE4, 0xBD}, true))
	if !errors.Is(err, errUTF8) {
		t.Fatalf("dangling lead: err = %v", err)
	}

	// A half character that completes only when the *next* fragment arrives is
	// legal mid-message: End must only be checked on the final continuation.
	r = NewReassembler(ReassemblerConfig{})
	if _, err = r.HandleFrame(frag(OpText, []byte{0xE4, 0xBD})); err != nil {
		t.Fatal(err)
	}
	if _, err = r.HandleFrame(cont([]byte{0xA0, '!'}, true)); err != nil {
		t.Fatalf("split character should reassemble: %v", err)
	}
}

func TestMessageSizeLimit(t *testing.T) {
	r := NewReassembler(ReassemblerConfig{MaxMessage: 10})

	// Single oversized frame.
	_, err := r.HandleFrame(fin(OpBinary, make([]byte, 11)))
	if !errors.Is(err, errMessageTooLarge) || CloseCode(err) != 1009 {
		t.Fatalf("single oversize: err=%v code=%d", err, CloseCode(err))
	}

	// Oversized via fragments: exactly the limit is fine.
	if _, err = r.HandleFrame(frag(OpBinary, make([]byte, 6))); err != nil {
		t.Fatal(err)
	}
	ev, err := r.HandleFrame(cont(make([]byte, 4), true))
	if err != nil || len(ev.Payload) != 10 {
		t.Fatalf("exactly-at-limit message: ev=%v err=%v", ev, err)
	}

	// One byte over via a continuation must fail.
	if _, err = r.HandleFrame(frag(OpBinary, make([]byte, 6))); err != nil {
		t.Fatal(err)
	}
	if _, err = r.HandleFrame(cont(make([]byte, 5), true)); !errors.Is(err, errMessageTooLarge) {
		t.Fatalf("fragmented oversize: err = %v", err)
	}
}

func TestClientMustMask(t *testing.T) {
	r := NewReassembler(ReassemblerConfig{RequireMask: true})
	f := fin(OpText, []byte("x"))
	f.Masked = false
	_, err := r.HandleFrame(f)
	if !errors.Is(err, errClientMustMask) || CloseCode(err) != 1002 {
		t.Fatalf("unmasked: err=%v code=%d", err, CloseCode(err))
	}
}

func TestRSVBitsRejected(t *testing.T) {
	r := NewReassembler(ReassemblerConfig{})
	f := fin(OpText, []byte("x"))
	f.RSV = 0x4
	_, err := r.HandleFrame(f)
	if !errors.Is(err, errBadRSV) {
		t.Fatalf("RSV err = %v", err)
	}
}

func TestReservedOpcodes(t *testing.T) {
	r := NewReassembler(ReassemblerConfig{})
	for _, op := range []byte{0x3, 0x7, 0xB, 0xF} {
		_, err := r.HandleFrame(fin(op, []byte("x")))
		if err == nil || CloseCode(err) != 1002 {
			t.Fatalf("opcode %#x: err=%v", op, err)
		}
	}
}

func TestCloseFrames(t *testing.T) {
	tests := []struct {
		name    string
		payload []byte
		code    int
		wantErr bool
	}{
		{"empty body", nil, 1005, false},
		{"normal 1000", []byte{0x03, 0xE8}, 1000, false},
		{"1001 with reason", append([]byte{0x03, 0xE9}, []byte("bye")...), 1001, false},
		{"private code 4000", []byte{0x0F, 0xA0}, 4000, false},
		{"single byte", []byte{0x03}, 0, true},
		{"forbidden 1004", []byte{0x03, 0xEC}, 0, true},
		{"forbidden 1005", []byte{0x03, 0xED}, 0, true},
		{"forbidden 1006", []byte{0x03, 0xEE}, 0, true},
		{"forbidden 1015", []byte{0x03, 0xF7}, 0, true},
		{"code 999", []byte{0x03, 0xE7}, 0, true},
		{"non-utf8 reason", append([]byte{0x03, 0xE8}, 0xFF), 0, true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			r := NewReassembler(ReassemblerConfig{})
			ev, err := r.HandleFrame(fin(OpClose, tc.payload))
			if tc.wantErr {
				if !errors.Is(err, errClosePayload) || CloseCode(err) != 1007 {
					t.Fatalf("err=%v code=%d", err, CloseCode(err))
				}
				return
			}
			if err != nil {
				t.Fatalf("valid close rejected: %v", err)
			}
			if ev.Type != EventClose || ev.CloseCode != tc.code {
				t.Fatalf("event=%+v want code %d", ev, tc.code)
			}
		})
	}

	// Close while a fragmented message is open drops the partial message.
	r := NewReassembler(ReassemblerConfig{})
	if _, err := r.HandleFrame(frag(OpText, []byte("partial"))); err != nil {
		t.Fatal(err)
	}
	if _, err := r.HandleFrame(fin(OpClose, []byte{0x03, 0xE8})); err != nil {
		t.Fatalf("close during fragment: %v", err)
	}
	if r.fragActive {
		t.Fatal("fragment state must reset after close")
	}
}

// TestReassemblerRandomChunks verifies the acceptance invariant one level up:
// reassembling a random frame sequence all at once versus feeding it through
// the FrameParser in random-sized chunks produces the same message events.
func TestReassemblerRandomChunks(t *testing.T) {
	rng := rand.New(rand.NewSource(99))

	for iter := 0; iter < 100; iter++ {
		// Build one random "conversation": a mix of complete messages and
		// fragmented messages, optionally with interleaved pings.
		var frames []Frame
		nMsg := 1 + rng.Intn(8)
		for m := 0; m < nMsg; m++ {
			isText := rng.Intn(2) == 0
			var payload []byte
			if isText {
				// Valid UTF-8 (ASCII keeps it simple).
				n := rng.Intn(500)
				payload = make([]byte, n)
				for i := range payload {
					payload[i] = byte('a' + rng.Intn(26))
				}
			} else {
				payload = randomBytes(rng, rng.Intn(500))
			}
			op := OpBinary
			if isText {
				op = OpText
			}
			if rng.Intn(2) == 0 {
				frames = append(frames, fin(op, payload))
			} else {
				frames = append(frames, frag(op, payload))
				// split the SAME payload into continuations by reusing pieces
				frames = append(frames, cont(nil, true))
			}
			if rng.Intn(3) == 0 {
				frames = append(frames, fin(OpPing, []byte{byte(m)}))
				frames = append(frames, fin(OpPong, nil))
			}
		}

		// Path A: frames straight into a reassembler.
		eventsA := drainFrames(t, frames, false)

		// Path B: encode all frames, slice into random chunks, parse, reassemble.
		var wire []byte
		for _, f := range frames {
			wire = append(wire, mustEncode(t, f, true)...)
		}
		eventsB := drainWire(t, wire, rng)

		if len(eventsA) != len(eventsB) {
			t.Fatalf("iter %d: %d events vs %d", iter, len(eventsA), len(eventsB))
		}
		for i := range eventsA {
			a, b := eventsA[i], eventsB[i]
			if a.Type != b.Type || a.OpCode != b.OpCode || string(a.Payload) != string(b.Payload) {
				t.Fatalf("iter %d event %d mismatch:\n A=%+v\n B=%+v", iter, i, a, b)
			}
		}
	}
}

func drainFrames(t *testing.T, frames []Frame, requireMask bool) []Event {
	t.Helper()
	r := NewReassembler(ReassemblerConfig{RequireMask: requireMask, MaxMessage: 1 << 20})
	var out []Event
	for _, f := range frames {
		ev, err := r.HandleFrame(f)
		if err != nil {
			t.Fatalf("drainFrames: %v", err)
		}
		if ev != nil && ev.Type != EventMessage {
			out = append(out, *ev)
		} else if ev != nil {
			out = append(out, *ev)
		}
	}
	return out
}

func drainWire(t *testing.T, wire []byte, rng *rand.Rand) []Event {
	t.Helper()
	p := NewFrameParser(0)
	r := NewReassembler(ReassemblerConfig{RequireMask: true, MaxMessage: 1 << 20})
	var out []Event
	emit := func(frames []Frame) {
		for _, f := range frames {
			ev, err := r.HandleFrame(f)
			if err != nil {
				t.Fatalf("drainWire: %v", err)
			}
			if ev != nil {
				out = append(out, *ev)
			}
		}
	}
	for pos := 0; pos < len(wire); {
		n := 1 + rng.Intn(32)
		if pos+n > len(wire) {
			n = len(wire) - pos
		}
		frames, err := p.Feed(wire[pos : pos+n])
		if err != nil {
			t.Fatalf("drainWire parse: %v", err)
		}
		emit(frames)
		pos += n
	}
	return out
}
