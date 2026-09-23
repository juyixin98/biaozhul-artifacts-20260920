package dnsmsg

import (
	"encoding/binary"
	"errors"
	"net"
	"testing"
)

// buildResponse assembles a minimal valid single-question/single-A-answer
// response using a compression pointer (0xC00C) for the answer owner.
func buildResponse(id uint16, name string, ttl uint32, ip net.IP, flags uint16) []byte {
	qname, _ := EncodeName(name)
	b := make([]byte, 0, 64)
	hdr := make([]byte, HeaderLen)
	binary.BigEndian.PutUint16(hdr[0:2], id)
	if flags == 0 {
		flags = 0x8180
	}
	binary.BigEndian.PutUint16(hdr[2:4], flags)
	binary.BigEndian.PutUint16(hdr[4:6], 1)
	binary.BigEndian.PutUint16(hdr[6:8], 1)
	b = append(b, hdr...)
	b = append(b, qname...)
	b = appendU16(b, TypeA)
	b = appendU16(b, ClassIN)
	// answer
	b = appendU16(b, 0xc00c)
	b = appendU16(b, TypeA)
	b = appendU16(b, ClassIN)
	b = appendU32(b, ttl)
	b = appendU16(b, 4)
	b = append(b, ip.To4()...)
	return b
}

func appendU16(b []byte, v uint16) []byte {
	return append(b, byte(v>>8), byte(v))
}
func appendU32(b []byte, v uint32) []byte {
	return append(b, byte(v>>24), byte(v>>16), byte(v>>8), byte(v))
}

func TestParseValidCompressedResponse(t *testing.T) {
	pkt := buildResponse(0x1234, "www.Example.COM.", 60, net.IPv4(192, 0, 2, 1), 0)
	m, _, err := ParseMessage(pkt)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if m.ID != 0x1234 {
		t.Fatalf("id = %x", m.ID)
	}
	if !m.QR || m.RCode != 0 || m.TC {
		t.Fatalf("flags wrong: %+v", m)
	}
	if len(m.Questions) != 1 || m.Questions[0].Name != "www.example.com" {
		t.Fatalf("question not canonicalized: %+v", m.Questions)
	}
	if len(m.Answers) != 1 {
		t.Fatalf("answers = %d", len(m.Answers))
	}
	a := m.Answers[0]
	if a.Name != "www.example.com" || a.TTL != 60 || net.IP(a.IP).String() != "192.0.2.1" {
		t.Fatalf("answer wrong: %+v ip=%v", a, a.IP)
	}
}

// A non-compressed name followed by more data must report the next offset
// correctly (needed for question parsing).
func TestDecodeNameNextOffset(t *testing.T) {
	msg := []byte{3, 'w', 'w', 'w', 7, 'e', 'x', 'a', 'm', 'p', 'l', 'e', 3, 'c', 'o', 'm', 0, 0xAA}
	name, next, err := DecodeName(msg, 0)
	if err != nil {
		t.Fatal(err)
	}
	if name != "www.example.com" {
		t.Fatalf("name=%q", name)
	}
	if next != 17 {
		t.Fatalf("next=%d want 17", next)
	}
}

// A name ending in a compression pointer reports the offset after the pointer.
func TestDecodeNamePointerNextOffset(t *testing.T) {
	target := []byte{7, 'e', 'x', 'a', 'm', 'p', 'l', 'e', 3, 'c', 'o', 'm', 0}
	msg := append(make([]byte, 4), target...)
	msg = append(msg, 3, 'f', 'o', 'o', 0xC0, 0x04)
	off := 4 + len(target)
	name, next, err := DecodeName(msg, off)
	if err != nil {
		t.Fatal(err)
	}
	if name != "foo.example.com" {
		t.Fatalf("name=%q", name)
	}
	if next != len(msg) {
		t.Fatalf("next=%d want %d", next, len(msg))
	}
}

// --- Pointer attacks --------------------------------------------------------

func TestSelfPointerIsRejected(t *testing.T) {
	// Question name at offset 12 is a pointer to itself (12 >= 12).
	msg := buildResponse(1, "ignored.example", 10, net.IPv4(1, 2, 3, 4), 0)
	msg[12] = 0xC0
	msg[13] = 0x0C
	if _, _, err := ParseMessage(msg); !errors.Is(err, ErrForwardPointer) {
		t.Fatalf("want ErrForwardPointer, got %v", err)
	}
}

func TestMutualPointerRingIsRejected(t *testing.T) {
	// Two-node ring at offsets 12 and 14: 12 -> 14 (forward), 14 -> 12.
	msg := buildResponse(1, "ignored.example", 10, net.IPv4(1, 2, 3, 4), 0)
	msg[12] = 0xC0
	msg[13] = 0x0E
	msg[14] = 0xC0
	msg[15] = 0x0C
	if _, _, err := ParseMessage(msg); !errors.Is(err, ErrForwardPointer) {
		t.Fatalf("ring: want ErrForwardPointer (forward edge), got %v", err)
	}
}

func TestPointerPastEndOfMessage(t *testing.T) {
	msg := buildResponse(1, "ignored.example", 10, net.IPv4(1, 2, 3, 4), 0)
	msg[12] = 0xC0
	msg[13] = 0x40 // offset 64, packet is ~37 bytes
	if _, _, err := ParseMessage(msg); !errors.Is(err, ErrBadPointer) {
		t.Fatalf("want ErrBadPointer, got %v", err)
	}
}

func TestPointerToTruncatedName(t *testing.T) {
	// A valid backward pointer whose target label overruns the packet.
	// Layout: target at 0 is a plain label length 5 with only 2 bytes left;
	// a pointer at the end jumps back to it.
	msg := []byte{5, 'a', 'b', 0xC0, 0x00}
	if _, _, err := DecodeName(msg, 3); !errors.Is(err, ErrTruncatedLabel) {
		t.Fatalf("want ErrTruncatedLabel, got %v", err)
	}
}

func TestLongBackwardChainTripsCap(t *testing.T) {
	// Independently reconstruct a 128-jump strictly-backward pointer chain
	// (127 chain nodes + the entry pointer) so the parser test does not
	// depend on the fakeserver package.
	msg, nameOff := makeLongChainPacket(127)
	if _, _, err := DecodeName(msg, nameOff); !errors.Is(err, ErrCompressionLoop) {
		t.Fatalf("want ErrCompressionLoop after 128 jumps, got %v", err)
	}
}

// makeLongChainPacket builds a message with a backward pointer chain. It
// returns the packet and the offset where the final "additional RR name"
// begins (the entry point that walks the chain).
func makeLongChainPacket(jumps int) ([]byte, int) {
	b := make([]byte, 0, 400)
	hdr := make([]byte, HeaderLen)
	b = append(b, hdr...) // counts all zero
	for len(b)%2 != 0 {
		b = append(b, 0)
	}
	root := len(b)
	b = append(b, 0)
	if len(b)%2 != 0 {
		b = append(b, 0)
	}
	first := len(b)
	for i := 0; i < jumps; i++ {
		target := root
		if i > 0 {
			target = first + 2*(i-1)
		}
		b = appendU16(b, 0xc000|uint16(target))
	}
	top := first + 2*(jumps-1)
	nameOff := len(b)
	b = appendU16(b, 0xc000|uint16(top))
	return b, nameOff
}

// A chain just inside the cap (126 chain nodes + entry pointer = 127 jumps)
// must succeed and resolve to the root name.
func TestChainAtLimitSucceeds(t *testing.T) {
	msg, nameOff := makeLongChainPacket(126)
	name, _, err := DecodeName(msg, nameOff)
	if err != nil {
		t.Fatalf("127-jump chain should be accepted, got %v", err)
	}
	if name != "." {
		t.Fatalf("chain name = %q, want root", name)
	}
}

// --- Truncation and reserved encodings --------------------------------------

func TestShortMessage(t *testing.T) {
	for _, n := range []int{0, 1, 11} {
		if _, _, err := ParseMessage(make([]byte, n)); !errors.Is(err, ErrShortMessage) {
			t.Fatalf("len %d: want ErrShortMessage, got %v", n, err)
		}
	}
}

func TestTruncatedQuestionQType(t *testing.T) {
	// Header + zero-length name, but no trailing qtype/qclass.
	msg := make([]byte, HeaderLen+1)
	binary.BigEndian.PutUint16(msg[4:6], 1)
	msg[12] = 0
	if _, _, err := ParseMessage(msg); !errors.Is(err, ErrTruncatedRR) {
		t.Fatalf("want ErrTruncatedRR, got %v", err)
	}
}

func TestTruncatedRRFixedPart(t *testing.T) {
	pkt := buildResponse(1, "example.com", 10, net.IPv4(1, 2, 3, 4), 0)
	// Drop the last 5 bytes of the answer fixed part/rdata.
	if _, _, err := ParseMessage(pkt[:len(pkt)-5]); !errors.Is(err, ErrTruncatedRR) {
		t.Fatalf("want ErrTruncatedRR, got %v", err)
	}
}

func TestRDLengthOverrun(t *testing.T) {
	pkt := buildResponse(1, "example.com", 10, net.IPv4(1, 2, 3, 4), 0)
	// Find rdlength (2 bytes before final 4 rdata bytes) and inflate it.
	off := len(pkt) - 6
	binary.BigEndian.PutUint16(pkt[off:off+2], 255)
	if _, _, err := ParseMessage(pkt); !errors.Is(err, ErrBadRDLength) {
		t.Fatalf("want ErrBadRDLength, got %v", err)
	}
}

func TestReservedLabelType10(t *testing.T) {
	msg := []byte{0x40, 0x00} // 01 prefix, reserved
	if _, _, err := DecodeName(msg, 0); !errors.Is(err, ErrReservedLabel) {
		t.Fatalf("want ErrReservedLabel, got %v", err)
	}
}

func TestReservedLabelType01(t *testing.T) {
	msg := []byte{0x80, 0x00} // 10 prefix, reserved
	if _, _, err := DecodeName(msg, 0); !errors.Is(err, ErrReservedLabel) {
		t.Fatalf("want ErrReservedLabel, got %v", err)
	}
}

func TestNameTooLong(t *testing.T) {
	// 85 plain labels of 4 wire bytes each = 340 bytes plus root -> > 255.
	msg := make([]byte, 0, 400)
	for i := 0; i < 85; i++ {
		msg = append(msg, 3, 'a', 'b', 'c')
	}
	msg = append(msg, 0)
	if _, _, err := DecodeName(msg, 0); !errors.Is(err, ErrNameTooLong) {
		t.Fatalf("want ErrNameTooLong, got %v", err)
	}
}

func TestRootName(t *testing.T) {
	name, next, err := DecodeName([]byte{0}, 0)
	if err != nil || name != "." || next != 1 {
		t.Fatalf("root: name=%q next=%d err=%v", name, next, err)
	}
}

// --- Builders ---------------------------------------------------------------

func TestBuildQueryRoundTrip(t *testing.T) {
	for _, qt := range []uint16{TypeA, TypeAAAA} {
		q, err := BuildQuery(0xabcd, "WWW.Example.COM", qt)
		if err != nil {
			t.Fatal(err)
		}
		m, _, err := ParseMessage(q)
		if err != nil {
			t.Fatal(err)
		}
		if m.QR || m.ID != 0xabcd || !m.RD {
			t.Fatalf("flags/id wrong: %+v", m)
		}
		if len(m.Questions) != 1 {
			t.Fatalf("qd=%d", len(m.Questions))
		}
		qq := m.Questions[0]
		if qq.Name != "www.example.com" || qq.Type != qt || qq.Class != ClassIN {
			t.Fatalf("question wrong: %+v", qq)
		}
	}
}

func TestBuildQueryRejectsBadTypeAndName(t *testing.T) {
	if _, err := BuildQuery(1, "example.com", 15); !errors.Is(err, ErrBadType) {
		t.Fatalf("type: want ErrBadType, got %v", err)
	}
	for _, bad := range []string{"", ".example.com", "ex..com", "verylongnamethatwayexceedssixtythreecharactersxxxxxxxxxxxxxxxxxxx.com", "bad\x00name.com"} {
		if _, err := BuildQuery(1, bad, TypeA); !errors.Is(err, ErrBadName) {
			t.Fatalf("name %q: want ErrBadName, got %v", bad, err)
		}
	}
}

func TestNewIDRangeAndRandom(t *testing.T) {
	seen := map[uint16]bool{}
	for i := 0; i < 1000; i++ {
		id, err := NewID()
		if err != nil {
			t.Fatal(err)
		}
		if id == 0 {
			t.Fatal("id must not be zero")
		}
		seen[id] = true
	}
	if len(seen) < 900 {
		t.Fatalf("NewID looks non-random: %d unique in 1000", len(seen))
	}
}
