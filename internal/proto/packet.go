// Package proto defines the wire format of the reliable-UDP simulation
// protocol and the modulo-2^32 sequence-number comparison.
package proto

import (
	"encoding/binary"
	"errors"
)

// Magic identifies packets of this protocol ("RP" = reliable pipe).
const Magic uint16 = 0x5250

// HeaderSize is the fixed packet header length in bytes:
//
//	0:2   magic
//	2:6   generation (connection generation, see below)
//	6     packet type
//	7     flags
//	8:12  sequence number (meaning depends on type)
//	12:16 reserved (zero)
const HeaderSize = 16

// Packet types.
const (
	TypeData    uint8 = 1 // Seq = absolute chunk sequence number, Payload = file chunk
	TypeAck     uint8 = 2 // Seq = cumulative next-expected sequence number
	TypeFin     uint8 = 3 // Seq = InitialSeq+total chunks, Payload = 32-byte SHA-256 of the file
	TypeDone    uint8 = 4 // Flags&FlagOK: receiver's hash matched the FIN hash
	TypeDoneAck uint8 = 5 // sender's final acknowledgement of Done
)

// FlagOK marks a Done packet whose receiver-side hash verification passed.
const FlagOK uint8 = 1

// ErrBadPacket is returned for malformed or foreign packets.
var ErrBadPacket = errors.New("malformed packet")

// Packet is one protocol message. Payload is owned by the Packet.
type Packet struct {
	Gen     uint32 // connection generation: packets of older/other connections are dropped
	Type    uint8
	Flags   uint8
	Seq     uint32
	Payload []byte
}

// Marshal encodes the packet to a freshly allocated byte slice.
func (p Packet) Marshal() []byte {
	b := make([]byte, HeaderSize+len(p.Payload))
	binary.BigEndian.PutUint16(b[0:2], Magic)
	binary.BigEndian.PutUint32(b[2:6], p.Gen)
	b[6] = p.Type
	b[7] = p.Flags
	binary.BigEndian.PutUint32(b[8:12], p.Seq)
	copy(b[HeaderSize:], p.Payload)
	return b
}

// Unmarshal decodes a packet, copying the payload.
func Unmarshal(b []byte) (Packet, error) {
	if len(b) < HeaderSize {
		return Packet{}, ErrBadPacket
	}
	if binary.BigEndian.Uint16(b[0:2]) != Magic {
		return Packet{}, ErrBadPacket
	}
	p := Packet{
		Gen:   binary.BigEndian.Uint32(b[2:6]),
		Type:  b[6],
		Flags: b[7],
		Seq:   binary.BigEndian.Uint32(b[8:12]),
	}
	if len(b) > HeaderSize {
		p.Payload = append([]byte(nil), b[HeaderSize:]...)
	}
	return p, nil
}

// SeqLess reports whether a precedes b in modulo-2^32 sequence space,
// using the RFC 1982 serial-number comparison: a < b iff (a-b) interpreted
// as a signed 32-bit integer is negative. Valid while the two numbers are
// less than 2^31 apart, which the window bound (<< 2^31) guarantees.
func SeqLess(a, b uint32) bool { return int32(a-b) < 0 }

// SeqLessEq is SeqLess or equal.
func SeqLessEq(a, b uint32) bool { return int32(a-b) <= 0 }
