package ws

import (
	"crypto/rand"
	"encoding/binary"
)

// EncodeFrame serialises one frame per RFC 6455 §5.2.
//
// masked=true generates a random masking key and masks the payload, as every
// client frame must be masked. Server frames are written with masked=false.
func EncodeFrame(f Frame, masked bool) ([]byte, error) {
	if f.IsControl() && len(f.Payload) > MaxControlPayload {
		return nil, protoErr("control payload of %d bytes exceeds 125", len(f.Payload))
	}
	if f.IsControl() && !f.Fin {
		return nil, errControlFragment
	}

	b0 := f.OpCode & 0x0F
	if f.Fin {
		b0 |= 0x80
	}
	b0 |= (f.RSV & 0x07) << 4

	n := len(f.Payload)
	var header [14]byte // 2 + max(8) + max(4)
	header[0] = b0
	hLen := 2

	switch {
	case n <= 125:
		header[1] = byte(n)
	case n <= 0xFFFF:
		header[1] = 126
		binary.BigEndian.PutUint16(header[2:4], uint16(n))
		hLen = 4
	default:
		header[1] = 127
		binary.BigEndian.PutUint64(header[2:10], uint64(n))
		hLen = 10
	}

	if masked {
		header[1] |= 0x80
		var key [4]byte
		if _, err := rand.Read(key[:]); err != nil {
			return nil, err
		}
		copy(header[hLen:hLen+4], key[:])
		hLen += 4
		out := make([]byte, hLen+n)
		copy(out, header[:hLen])
		copy(out[hLen:], f.Payload)
		for i := range f.Payload {
			out[hLen+i] ^= key[i%4]
		}
		return out, nil
	}

	out := make([]byte, hLen+n)
	copy(out, header[:hLen])
	copy(out[hLen:], f.Payload)
	return out, nil
}

// EncodeMessage is a convenience wrapper for a single, FIN, un-fragmented frame.
func EncodeMessage(opcode byte, payload []byte, masked bool) ([]byte, error) {
	return EncodeFrame(Frame{Fin: true, OpCode: opcode, Payload: payload}, masked)
}
