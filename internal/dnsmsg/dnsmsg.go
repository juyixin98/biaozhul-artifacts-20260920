// Package dnsmsg parses and builds the small subset of DNS wire messages
// (RFC 1035) used by the proxy: UDP, a single question, A/AAAA answers.
//
// The parser is hardened against hostile compression pointers: pointers may
// only jump backwards within the message, jump chains are bounded, and every
// read is bounds-checked. See DecodeName for details.
package dnsmsg

import (
	"crypto/rand"
	"encoding/binary"
	"errors"
	"fmt"
	"strings"
)

const (
	HeaderLen = 12  // bytes in a fixed DNS header
	MaxName   = 255 // RFC 1035: max length of an encoded domain name
	MaxLabel  = 63  // RFC 1035: max length of one label

	// maxPointers bounds compression-pointer chains. RFC 1035 allows
	// arbitrary chains, so a hard cap is needed to make pointer loops and
	// pathological chain lengths unable to consume unbounded work. A chain
	// of 127 jumps is accepted; the 128th is rejected.
	maxPointers = 128
	// maxRRCount caps the number of records accepted per section so a packet
	// claiming a huge count cannot force a huge allocation.
	maxRRCount = 4096

	// Question types.
	TypeA    uint16 = 1
	TypeAAAA uint16 = 28
	ClassIN  uint16 = 1

	// Response codes.
	RCodeOK       = 0
	RCodeFormErr  = 1
	RCodeServFail = 2
	RCodeNXDomain = 3
	RCodeNotImpl  = 4
	RCodeRefused  = 5
)

// Errors returned by the parser. They are sentinel values wrapped with context.
var (
	ErrShortMessage    = errors.New("dnsmsg: message shorter than 12-byte header")
	ErrNameTooLong     = errors.New("dnsmsg: decoded domain name longer than 255 octets")
	ErrTruncatedLabel  = errors.New("dnsmsg: label runs past end of message")
	ErrBadPointer      = errors.New("dnsmsg: compression pointer out of bounds")
	ErrForwardPointer  = errors.New("dnsmsg: compression pointer does not point backwards")
	ErrCompressionLoop = errors.New("dnsmsg: compression pointer chain too long (loop or >127 jumps)")
	ErrReservedLabel   = errors.New("dnsmsg: label length uses reserved 10/01 type bits")
	ErrTruncatedRR     = errors.New("dnsmsg: resource record truncated")
	ErrBadCount        = errors.New("dnsmsg: section count exceeds safe cap")
	ErrBadRDLength     = errors.New("dnsmsg: rdata length exceeds message")
	ErrBadName         = errors.New("dnsmsg: invalid domain name")
	ErrBadType         = errors.New("dnsmsg: unsupported question type")
)

// Question is one DNS question section entry.
type Question struct {
	Name  string // dotted, trailing-dot-free, lowercase; "." for root
	Type  uint16
	Class uint16
}

// ResourceRecord is one RR from answer/authority/additional sections. For
// A/AAAA records, IP holds the address bytes (4 or 16). RData keeps the raw
// bytes of every record regardless of type.
type ResourceRecord struct {
	Name  string
	Type  uint16
	Class uint16
	TTL   uint32
	IP    []byte // 4 or 16 bytes for A/AAAA, nil otherwise
	RData []byte
}

// Message is a decoded DNS message.
type Message struct {
	ID         uint16
	QR         bool
	Opcode     uint8
	AA         bool
	TC         bool
	RD         bool
	RA         bool
	RCode      uint8
	Questions  []Question
	Answers    []ResourceRecord
	Authority  []ResourceRecord
	Additional []ResourceRecord
}

// IsResponse reports whether the QR bit is set.
func (m *Message) IsResponse() bool { return m.QR }

// DecodeName reads a <character-string>-style domain name starting at off in
// msg. It returns the decoded name ("." for the root, lowercase, no trailing
// dot) and the offset immediately following the name. When the name ends in a
// compression pointer, the returned "next" offset is the position after the
// two-byte pointer (as required by RFC 1035 4.1.4), not the end of the
// pointed-to name.
//
// Safety properties enforced here:
//   - Every byte read is bounds-checked against len(msg).
//   - A pointer target must point strictly backwards and inside the message.
//     Because the target is always behind the pointer itself, a chain cannot
//     revisit a position via only legal jumps; forward/self pointers (which is
//     what a real pointer cycle requires) are rejected outright.
//   - A visited-set and a 127-jump cap provide defense in depth against any
//     remaining cycle.
//   - Reserved length-prefix types (the 10 and 01 combinations of the top two
//     bits) are rejected. Only plain labels (00) and pointers (11) are legal.
//   - Decoded names are capped at 255 octets of wire length.
func DecodeName(msg []byte, off int) (string, int, error) {
	if off < 0 || off > len(msg) {
		return "", 0, ErrBadPointer
	}

	var (
		labels     []string
		pos        = off
		jumped     = false
		nextOff    int
		jumps      int
		decodedLen int // wire length the labels would occupy: sum(label+1)
		visited    map[int]struct{}
	)

	for {
		if pos >= len(msg) {
			return "", 0, ErrTruncatedLabel
		}
		b := msg[pos]
		switch {
		case b == 0:
			// Root label: end of name.
			if !jumped {
				nextOff = pos + 1
			}
			if len(labels) == 0 {
				return ".", nextOff, nil
			}
			return strings.Join(labels, "."), nextOff, nil

		case b&0xc0 == 0xc0:
			// Compression pointer (11xxxxxx xxxxxxxx).
			if pos+1 >= len(msg) {
				return "", 0, ErrTruncatedLabel
			}
			target := int(binary.BigEndian.Uint16(msg[pos:pos+2]) & 0x3fff)

			if !jumped {
				nextOff = pos + 2
			}
			// The pointer must point somewhere before itself and inside the
			// message. Pointing at/after the current offset would allow a
			// self or mutual pointer loop, so reject it.
			if target >= len(msg) {
				return "", 0, fmt.Errorf("%w: offset %d in %d-byte message", ErrBadPointer, target, len(msg))
			}
			if target >= pos {
				return "", 0, fmt.Errorf("%w: target %d >= position %d", ErrForwardPointer, target, pos)
			}
			jumps++
			if jumps >= maxPointers {
				return "", 0, ErrCompressionLoop
			}
			if visited == nil {
				visited = make(map[int]struct{})
			}
			if _, seen := visited[target]; seen {
				return "", 0, ErrCompressionLoop
			}
			visited[target] = struct{}{}
			pos = target
			jumped = true

		case b&0xc0 == 0:
			// Plain label (00xxxxxx); the top two bits being 00 bounds ln
			// to 0..63, so the RFC label-size limit needs no extra check.
			ln := int(b)
			if pos+1+ln > len(msg) {
				return "", 0, ErrTruncatedLabel
			}
			decodedLen += 1 + ln
			if decodedLen > MaxName {
				return "", 0, ErrNameTooLong
			}
			label := strings.ToLower(string(msg[pos+1 : pos+1+ln]))
			labels = append(labels, label)
			pos += 1 + ln

		default:
			// Reserved types 10 (01) and 01 (10): never valid in a name.
			return "", 0, fmt.Errorf("%w: prefix byte 0x%02x", ErrReservedLabel, b)
		}
	}
}

// CanonicalName lowercases and strips a trailing dot. The input is assumed to
// be already encoded-name safe; validation happens in EncodeName.
func CanonicalName(name string) string {
	n := strings.ToLower(name)
	n = strings.TrimSuffix(n, ".")
	return n
}

// EncodeName encodes a dotted domain name into wire form, with validation.
// Accepts "example.com" or "example.com." and "." for the root.
func EncodeName(name string) ([]byte, error) {
	name = strings.TrimSpace(name)
	if name == "" {
		return nil, fmt.Errorf("%w: empty name", ErrBadName)
	}
	if name == "." {
		return []byte{0}, nil
	}
	name = strings.TrimSuffix(name, ".")
	if len(name) > MaxName-1 {
		return nil, fmt.Errorf("%w: name too long", ErrBadName)
	}
	var out []byte
	for _, label := range strings.Split(name, ".") {
		if len(label) == 0 {
			return nil, fmt.Errorf("%w: empty label in %q", ErrBadName, name)
		}
		if len(label) > MaxLabel {
			return nil, fmt.Errorf("%w: label %q longer than 63 octets", ErrBadName, label)
		}
		// Reject embedded NULs and other bytes that are not legal in a name
		// this proxy is willing to send.
		for i := 0; i < len(label); i++ {
			c := label[i]
			if c < 0x21 || c == 0x7f {
				return nil, fmt.Errorf("%w: illegal byte 0x%02x in label %q", ErrBadName, c, label)
			}
		}
		out = append(out, byte(len(label)))
		out = append(out, label...)
	}
	out = append(out, 0)
	if len(out) > MaxName {
		return nil, fmt.Errorf("%w: encoded name longer than 255 octets", ErrBadName)
	}
	return out, nil
}

// ParseMessage fully parses a DNS wire message, including all four sections.
func ParseMessage(msg []byte) (*Message, int, error) {
	if len(msg) < HeaderLen {
		return nil, 0, fmt.Errorf("%w: got %d bytes", ErrShortMessage, len(msg))
	}
	m := &Message{
		ID: binary.BigEndian.Uint16(msg[0:2]),
	}
	flags := binary.BigEndian.Uint16(msg[2:4])
	m.QR = flags&0x8000 != 0
	m.Opcode = uint8((flags >> 11) & 0x0f)
	m.AA = flags&0x0400 != 0
	m.TC = flags&0x0200 != 0
	m.RD = flags&0x0100 != 0
	m.RA = flags&0x0080 != 0
	m.RCode = uint8(flags & 0x000f)

	qdCount := binary.BigEndian.Uint16(msg[4:6])
	anCount := binary.BigEndian.Uint16(msg[6:8])
	nsCount := binary.BigEndian.Uint16(msg[8:10])
	arCount := binary.BigEndian.Uint16(msg[10:12])

	if qdCount > maxRRCount || anCount > maxRRCount || nsCount > maxRRCount || arCount > maxRRCount {
		return nil, 0, fmt.Errorf("%w: qd=%d an=%d ns=%d ar=%d", ErrBadCount, qdCount, anCount, nsCount, arCount)
	}

	pos := HeaderLen

	m.Questions = make([]Question, 0, qdCount)
	for i := 0; i < int(qdCount); i++ {
		n, next, err := DecodeName(msg, pos)
		if err != nil {
			return nil, 0, fmt.Errorf("question %d: %w", i, err)
		}
		pos = next
		if pos+4 > len(msg) {
			return nil, 0, fmt.Errorf("question %d: %w (qtype/qclass)", i, ErrTruncatedRR)
		}
		m.Questions = append(m.Questions, Question{
			Name:  n,
			Type:  binary.BigEndian.Uint16(msg[pos : pos+2]),
			Class: binary.BigEndian.Uint16(msg[pos+2 : pos+4]),
		})
		pos += 4
	}

	parseSection := func(n int) ([]ResourceRecord, error) {
		rrs := make([]ResourceRecord, 0, n)
		for i := 0; i < n; i++ {
			name, next, err := DecodeName(msg, pos)
			if err != nil {
				return nil, fmt.Errorf("rr %d name: %w", i, err)
			}
			pos = next
			// Fixed part: type(2) class(2) ttl(4) rdlength(2) = 10 bytes.
			if pos+10 > len(msg) {
				return nil, fmt.Errorf("rr %d: %w", i, ErrTruncatedRR)
			}
			rr := ResourceRecord{
				Name:  name,
				Type:  binary.BigEndian.Uint16(msg[pos : pos+2]),
				Class: binary.BigEndian.Uint16(msg[pos+2 : pos+4]),
				TTL:   binary.BigEndian.Uint32(msg[pos+4 : pos+8]),
			}
			rdLen := int(binary.BigEndian.Uint16(msg[pos+8 : pos+10]))
			pos += 10
			if rdLen > len(msg)-pos {
				return nil, fmt.Errorf("rr %d: %w: rdlength=%d remaining=%d", i, ErrBadRDLength, rdLen, len(msg)-pos)
			}
			rr.RData = make([]byte, rdLen)
			copy(rr.RData, msg[pos:pos+rdLen])
			if (rr.Type == TypeA && rdLen == 4) || (rr.Type == TypeAAAA && rdLen == 16) {
				rr.IP = make([]byte, rdLen)
				copy(rr.IP, rr.RData)
			}
			rrs = append(rrs, rr)
			pos += rdLen
		}
		return rrs, nil
	}

	var err error
	if m.Answers, err = parseSection(int(anCount)); err != nil {
		return nil, 0, fmt.Errorf("answer section: %w", err)
	}
	if m.Authority, err = parseSection(int(nsCount)); err != nil {
		return nil, 0, fmt.Errorf("authority section: %w", err)
	}
	if m.Additional, err = parseSection(int(arCount)); err != nil {
		return nil, 0, fmt.Errorf("additional section: %w", err)
	}
	if pos != len(msg) {
		// Trailing garbage is tolerated by real servers occasionally; keep
		// parsing strict but only informational.
		return m, pos, nil
	}
	return m, pos, nil
}

// BuildQuery constructs a standard query: RD=1, exactly one IN question.
func BuildQuery(id uint16, qname string, qtype uint16) ([]byte, error) {
	if qtype != TypeA && qtype != TypeAAAA {
		return nil, fmt.Errorf("%w: %d (only A=1 and AAAA=28)", ErrBadType, qtype)
	}
	enc, err := EncodeName(qname)
	if err != nil {
		return nil, err
	}
	msg := make([]byte, 0, HeaderLen+len(enc)+4)
	hdr := make([]byte, HeaderLen)
	binary.BigEndian.PutUint16(hdr[0:2], id)
	// QR=0 Opcode=0 RD=1, everything else zero.
	binary.BigEndian.PutUint16(hdr[2:4], 0x0100)
	binary.BigEndian.PutUint16(hdr[4:6], 1) // QDCOUNT=1
	msg = append(msg, hdr...)
	msg = append(msg, enc...)
	qtail := make([]byte, 4)
	binary.BigEndian.PutUint16(qtail[0:2], qtype)
	binary.BigEndian.PutUint16(qtail[2:4], ClassIN)
	msg = append(msg, qtail...)
	return msg, nil
}

// NewID returns a random DNS transaction ID in [1, 65535]. crypto/rand is used
// so upstream-sourced answers cannot be predicted/spoofed by guessable IDs.
func NewID() (uint16, error) {
	var b [2]byte
	if _, err := rand.Read(b[:]); err != nil {
		return 0, err
	}
	id := binary.BigEndian.Uint16(b[:])
	if id == 0 {
		id = 1
	}
	return id, nil
}
