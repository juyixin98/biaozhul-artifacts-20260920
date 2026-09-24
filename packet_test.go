package rt

import (
	"bytes"
	"testing"
)

func TestSeqComparison(t *testing.T) {
	cases := []struct {
		a, b uint32
		less bool
	}{
		{0, 1, true},
		{1, 0, false},
		{5, 5, false},
		// 回绕：0xFFFFFFFF 比 0 “旧”（环上差 1）
		{0xFFFFFFFF, 0, true},
		{0, 0xFFFFFFFF, false},
		{0xFFFFFFF0, 0x00000010, true},  // 跨回绕正向 32
		{0x00000010, 0xFFFFFFF0, false}, // 反向 32
		{0, seqHalf, false},             // 正好半圈：不算小于
		{seqHalf, 0, false},
	}
	for _, c := range cases {
		if got := seqLess(c.a, c.b); got != c.less {
			t.Errorf("seqLess(%d,%d)=%v want %v", c.a, c.b, got, c.less)
		}
		if c.a != c.b && seqLE(c.a, c.b) != c.less {
			t.Errorf("seqLE(%d,%d) mismatch", c.a, c.b)
		}
	}
	if !seqLE(5, 5) {
		t.Error("seqLE(5,5) should be true")
	}
}

func TestSeqInWindow(t *testing.T) {
	// 窗口 [0xFFFFFFFC, +4) = FFC FFD FFE FFF
	base := uint32(0xFFFFFFFC)
	for _, x := range []uint32{0xFFFFFFFC, 0xFFFFFFFD, 0xFFFFFFFE, 0xFFFFFFFF} {
		if !seqInWindow(base, x, 4) {
			t.Errorf("seq %08x should be in window", x)
		}
	}
	for _, x := range []uint32{0, 1, 0xFFFFFFFB} {
		if seqInWindow(base, x, 4) {
			t.Errorf("seq %08x should be outside window", x)
		}
	}
	if got := windowUsed(0xFFFFFFFE, 1); got != 3 {
		t.Errorf("windowUsed wrap = %d, want 3", got)
	}
}

func TestPacketMarshalRoundTrip(t *testing.T) {
	pkts := []*Packet{
		{Type: MsgSYN, Gen: 7, Payload: marshalSYN(0xFFFFFFFE, 512)},
		{Type: MsgDATA, Seq: 42, Ack: 42, Gen: 9, Payload: bytes.Repeat([]byte{0xAB}, 1500)},
		{Type: MsgACK, Ack: 0x80000000, Gen: 1},
		{Type: MsgFIN, Seq: 5, Gen: 3, Payload: marshalFIN(12345, bytes.Repeat([]byte{1}, 32))},
		{Type: MsgERROR, Gen: 3, Payload: []byte("hash mismatch")},
		{Type: MsgSYNACK, Gen: 2, Ack: 99},
	}
	for _, p := range pkts {
		b, err := p.Marshal()
		if err != nil {
			t.Fatal(err)
		}
		q, err := UnmarshalPacket(b)
		if err != nil {
			t.Fatal(err)
		}
		if q.Type != p.Type || q.Seq != p.Seq || q.Ack != p.Ack || q.Gen != p.Gen ||
			!bytes.Equal(q.Payload, p.Payload) {
			t.Fatalf("roundtrip mismatch: %+v vs %+v", p, q)
		}
	}

	if _, err := UnmarshalPacket(make([]byte, 10)); err == nil {
		t.Error("short packet should fail")
	}
}

func TestFINMetadata(t *testing.T) {
	sum := bytes.Repeat([]byte{0x77}, 32)
	b := marshalFIN(987654, sum)
	n, got, err := unmarshalFIN(b)
	if err != nil || n != 987654 || !bytes.Equal(got, sum) {
		t.Fatalf("FIN metadata mismatch: %d %v %v", n, got, err)
	}
}
