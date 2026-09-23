// Package ws implements a local, streaming WebSocket (RFC 6455) frame parser
// and a fragmented-message reassembler using only the Go standard library.
//
// The two streaming state machines are:
//
//   - FrameParser:  raw bytes -> decoded Frame values (client masking handled here)
//   - Reassembler:  Frame stream -> complete Message / control events, with
//     message size limits and cross-fragment UTF-8 validation
//
// Neither type uses a goroutine; Feed/HandleFrame are driven by the caller,
// which makes byte-at-a-time ("random chunk") testing straightforward.
package ws

import (
	"errors"
	"fmt"
	"io"
)

// Frame opcodes defined by RFC 6455 §5.2/§11.8.
const (
	OpContinuation byte = 0x0
	OpText         byte = 0x1
	OpBinary       byte = 0x2
	OpClose        byte = 0x8
	OpPing         byte = 0x9
	OpPong         byte = 0xA
)

// MaxControlPayload is the maximum payload length of a control frame (RFC 6455 §5.5).
const MaxControlPayload = 125

// Frame is one decoded WebSocket frame. Payload is owned by the Frame: the
// parser always allocates and copies (and un-masks) it, so a later Feed call
// never mutates it.
type Frame struct {
	Fin     bool
	RSV     byte // the three reserved bits, packed as bits 2,1,0
	OpCode  byte
	Masked  bool
	Payload []byte
}

// IsControl reports whether the opcode is a control opcode (0x8..0xF).
func (f Frame) IsControl() bool { return f.OpCode&0x8 != 0 }

// codedError pairs a human-readable message with the WebSocket close code
// (RFC 6455 §7.4.1) that a server should send when it occurs.
type codedError struct {
	code int
	msg  string
}

func (e *codedError) Error() string { return e.msg }

// CloseCode extracts the recommended close code from an error returned by this
// package. Unknown errors map to 1011 (internal error).
func CloseCode(err error) int {
	var ce *codedError
	if errors.As(err, &ce) {
		return ce.code
	}
	return 1011
}

func protoErr(format string, args ...any) error {
	return &codedError{code: 1002, msg: fmt.Sprintf(format, args...)}
}

func policyErr(format string, args ...any) error {
	return &codedError{code: 1009, msg: fmt.Sprintf(format, args...)}
}

func dataErr(format string, args ...any) error {
	return &codedError{code: 1007, msg: fmt.Sprintf(format, args...)}
}

// Frame-level errors. They are intentionally package-private; callers classify
// them with errors.Is and CloseCode.
var (
	errFrameTooLarge   = policyErr("frame payload exceeds configured maximum")
	errInvalidLength   = protoErr("payload length field does not use minimal encoding")
	errControlFragment = protoErr("control frame must not be fragmented")
	errBadRSV          = protoErr("RSV bits must be zero without an extension")
	errInvalidOpcode   = protoErr("reserved opcode is not defined")
	errClosePayload    = dataErr("invalid close frame payload")
	errUTF8            = dataErr("text message is not valid UTF-8")
	errMessageTooLarge = policyErr("reassembled message exceeds configured maximum")
	errMessageActive   = protoErr("new data frame started before previous fragmented message finished")
	errNoMessage       = protoErr("continuation frame without an active fragmented message")
	errClientMustMask  = protoErr("client frames must be masked (RFC 6455 §5.1)")
)

// FrameParser is a push-based, incremental WebSocket frame parser.
//
// Call Feed with whatever bytes are available right now — one byte, one TCP
// segment, or a whole pre-recorded byte stream. It returns the fully decoded
// frames contained in that input; a frame split across Feed calls is retained
// internally and completed by later calls. After an error the parser is dead:
// the wire state is undefined and the connection must be closed.
type FrameParser struct {
	maxPayload int
	buf        []byte
}

// NewFrameParser creates a parser that rejects any single frame whose payload
// exceeds maxPayload bytes. A non-positive value disables the limit.
func NewFrameParser(maxPayload int) *FrameParser {
	return &FrameParser{maxPayload: maxPayload}
}

// Feed appends data to the internal buffer and parses as many complete frames
// as possible. Returned Payload slices are independent of data, so the caller
// may reuse the input buffer afterwards.
func (p *FrameParser) Feed(data []byte) ([]Frame, error) {
	p.buf = append(p.buf, data...)
	var frames []Frame
	for {
		f, n, err := p.parseFrame(p.buf)
		if err != nil {
			if errors.Is(err, io.ErrUnexpectedEOF) {
				break // need more bytes
			}
			return frames, err
		}
		// Drop the consumed bytes and release the buffer once fully drained so a
		// large frame does not pin the capacity forever.
		p.buf = p.buf[n:]
		if len(p.buf) == 0 {
			p.buf = nil
		}
		frames = append(frames, f)
	}
	return frames, nil
}

// parseFrame attempts to decode one frame at the start of b. io.ErrUnexpectedEOF
// means the frame is incomplete and more bytes are required.
func (p *FrameParser) parseFrame(b []byte) (Frame, int, error) {
	if len(b) < 2 {
		return Frame{}, 0, io.ErrUnexpectedEOF
	}
	b0, b1 := b[0], b[1]
	f := Frame{
		Fin:    b0&0x80 != 0,
		RSV:    (b0 >> 4) & 0x07,
		OpCode: b0 & 0x0F,
		Masked: b1&0x80 != 0,
	}

	length := int(b1 & 0x7F)
	pos := 2
	switch {
	case length == 126:
		if len(b) < pos+2 {
			return Frame{}, 0, io.ErrUnexpectedEOF
		}
		length = int(b[pos])<<8 | int(b[pos+1])
		pos += 2
		if length < 126 {
			return Frame{}, 0, errInvalidLength
		}
	case length == 127:
		if len(b) < pos+8 {
			return Frame{}, 0, io.ErrUnexpectedEOF
		}
		// RFC 6455 §5.2: the most significant bit MUST be 0.
		if b[pos]&0x80 != 0 {
			return Frame{}, 0, protoErr("64-bit payload length exceeds signed range")
		}
		for i := 0; i < 8; i++ {
			length = length<<8 | int(b[pos+i])
		}
		pos += 8
		if length < 65536 {
			return Frame{}, 0, errInvalidLength
		}
	}

	if f.IsControl() && (!f.Fin || length > MaxControlPayload) {
		// Control frames are never fragmented and carry at most 125 bytes.
		if !f.Fin {
			return Frame{}, 0, errControlFragment
		}
		return Frame{}, 0, protoErr("control payload of %d bytes exceeds 125", length)
	}
	if p.maxPayload > 0 && length > p.maxPayload {
		return Frame{}, 0, errFrameTooLarge
	}

	var maskKey [4]byte
	if f.Masked {
		if len(b) < pos+4 {
			return Frame{}, 0, io.ErrUnexpectedEOF
		}
		copy(maskKey[:], b[pos:pos+4])
		pos += 4
	}

	if len(b) < pos+length {
		return Frame{}, 0, io.ErrUnexpectedEOF
	}

	// Copy so the Frame survives the caller reusing its buffer.
	payload := make([]byte, length)
	copy(payload, b[pos:pos+length])
	if f.Masked {
		for i := range payload {
			payload[i] ^= maskKey[i%4]
		}
	}
	f.Payload = payload
	return f, pos + length, nil
}
