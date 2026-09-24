package proto

import (
	"bytes"
	"testing"
)

func TestMarshalUnmarshalRoundTrip(t *testing.T) {
	orig := Packet{
		Gen:     0xDEADBEEF,
		Type:    TypeData,
		Flags:   FlagOK,
		Seq:     0xFFFFFFF0,
		Payload: []byte("hello reliable udp"),
	}
	got, err := Unmarshal(orig.Marshal())
	if err != nil {
		t.Fatalf("Unmarshal: %v", err)
	}
	if got.Gen != orig.Gen || got.Type != orig.Type || got.Flags != orig.Flags || got.Seq != orig.Seq {
		t.Fatalf("header mismatch: got %+v want %+v", got, orig)
	}
	if !bytes.Equal(got.Payload, orig.Payload) {
		t.Fatalf("payload mismatch: got %q want %q", got.Payload, orig.Payload)
	}
}

func TestUnmarshalEmptyPayload(t *testing.T) {
	p, err := Unmarshal(Packet{Gen: 7, Type: TypeAck, Seq: 9}.Marshal())
	if err != nil {
		t.Fatalf("Unmarshal: %v", err)
	}
	if len(p.Payload) != 0 {
		t.Fatalf("expected empty payload, got %d bytes", len(p.Payload))
	}
}

func TestUnmarshalRejectsBadPackets(t *testing.T) {
	if _, err := Unmarshal([]byte{1, 2, 3}); err != ErrBadPacket {
		t.Fatalf("short packet: got %v, want ErrBadPacket", err)
	}
	bad := Packet{Gen: 1, Type: TypeData}.Marshal()
	bad[0] ^= 0xFF // corrupt the magic
	if _, err := Unmarshal(bad); err != ErrBadPacket {
		t.Fatalf("bad magic: got %v, want ErrBadPacket", err)
	}
}

// TestSeqLessWrapAround pins the RFC 1982 serial-number comparison,
// including across the 2^32 wrap boundary.
func TestSeqLessWrapAround(t *testing.T) {
	cases := []struct {
		a, b uint32
		want bool
	}{
		{0, 1, true},
		{1, 0, false},
		{0, 0, false},
		{100, 200, true},
		{200, 100, false},
		{0xFFFFFFFF, 0, true},          // just before wrap < just after wrap
		{0, 0xFFFFFFFF, false},         // and not the reverse
		{0xFFFFFFFE, 2, true},          // across the boundary
		{2, 0xFFFFFFFE, false},         //
		{0xFFFFFF00, 0x000000FF, true}, // 255 steps across the boundary
		{0x000000FF, 0xFFFFFF00, false},
		{0, 0x7FFFFFFF, true}, // half the space apart: still ordered
		{0, 0x80000000, true}, // exactly half: undefined per RFC 1982; pin the
		// implementation's deterministic result (int32(a-b) < 0)
	}
	for _, c := range cases {
		if got := SeqLess(c.a, c.b); got != c.want {
			t.Errorf("SeqLess(%#x, %#x) = %v, want %v", c.a, c.b, got, c.want)
		}
		if c.a != c.b && SeqLess(c.a, c.b) == SeqLess(c.b, c.a) && c.b-c.a != 0x80000000 {
			t.Errorf("SeqLess(%#x, %#x) and its reverse agree", c.a, c.b)
		}
	}
}
