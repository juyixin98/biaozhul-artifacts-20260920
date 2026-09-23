package ws

import "unicode/utf8"

// EventType classifies an event emitted by the message reassembler.
type EventType int

const (
	// EventMessage is one fully reassembled text or binary message.
	EventMessage EventType = iota
	// EventPing / EventPong are control frames and may appear between fragments.
	EventPing
	EventPong
	// EventClose is a valid close frame. The closing handshake should start now.
	EventClose
)

// Event is one reassembler output.
//
// For EventMessage, Payload is the complete (possibly fragmented) message and
// ownership transfers to the caller: the reassembler reuses no part of it.
// Control-event payloads may alias the frame payload; treat them as read-only.
type Event struct {
	Type        EventType
	OpCode      byte   // OpText / OpBinary for EventMessage; control opcode otherwise
	Payload     []byte // message bytes, or ping/pong/close body
	CloseCode   int    // EventClose only; 1005 when the peer sent an empty body
	CloseReason string // EventClose only; valid UTF-8
}

// ReassemblerConfig configures a Reassembler. Zero values select defaults.
type ReassemblerConfig struct {
	// MaxMessage is the maximum reassembled message size in bytes. Default 1 MiB.
	MaxMessage int
	// RequireMask, when true, rejects unmasked frames (server side).
	RequireMask bool
}

// Reassembler is the fragmented-message state machine (RFC 6455 §5.4, §5.5).
// It validates continuation/control ordering, message size and text UTF-8.
type Reassembler struct {
	maxMessage  int
	requireMask bool

	fragActive bool
	fragBinary bool
	fragValid  bool // accumulated text is still valid UTF-8 so far
	msgBuf     []byte
	utf        utf8Validator
}

// NewReassembler creates a Reassembler.
func NewReassembler(cfg ReassemblerConfig) *Reassembler {
	if cfg.MaxMessage <= 0 {
		cfg.MaxMessage = 1 << 20
	}
	return &Reassembler{maxMessage: cfg.MaxMessage, requireMask: cfg.RequireMask}
}

// HandleFrame advances the state machine by one frame and returns at most one
// Event. A nil event means the frame was consumed into an in-progress message.
func (r *Reassembler) HandleFrame(f Frame) (*Event, error) {
	if r.requireMask && !f.Masked {
		return nil, errClientMustMask
	}
	if f.RSV != 0 {
		return nil, errBadRSV
	}

	if f.IsControl() {
		return r.handleControl(f)
	}

	switch f.OpCode {
	case OpText, OpBinary:
		if r.fragActive {
			return nil, errMessageActive
		}
		if f.Fin {
			if len(f.Payload) > r.maxMessage {
				return nil, errMessageTooLarge
			}
			if f.OpCode == OpText && !utf8.Valid(f.Payload) {
				return nil, errUTF8
			}
			return &Event{Type: EventMessage, OpCode: f.OpCode, Payload: f.Payload}, nil
		}
		// Start a fragmented message.
		r.fragActive = true
		r.fragBinary = f.OpCode == OpBinary
		r.msgBuf = append(r.msgBuf[:0], f.Payload...)
		if len(r.msgBuf) > r.maxMessage {
			return nil, errMessageTooLarge
		}
		r.utf.reset()
		if f.OpCode == OpText {
			r.fragValid = r.utf.write(f.Payload)
		} else {
			r.fragValid = true
		}
		return nil, nil

	case OpContinuation:
		if !r.fragActive {
			return nil, errNoMessage
		}
		if len(r.msgBuf)+len(f.Payload) > r.maxMessage {
			return nil, errMessageTooLarge
		}
		r.msgBuf = append(r.msgBuf, f.Payload...)
		if !r.fragBinary {
			if r.fragValid {
				r.fragValid = r.utf.write(f.Payload)
			}
		}
		if !f.Fin {
			return nil, nil
		}
		// Final continuation: finish and emit.
		if !r.fragBinary && (!r.fragValid || !r.utf.end()) {
			r.resetFragment()
			return nil, errUTF8
		}
		op := OpText
		if r.fragBinary {
			op = OpBinary
		}
		msg := r.msgBuf
		r.resetFragment()
		return &Event{Type: EventMessage, OpCode: op, Payload: msg}, nil

	default:
		// Opcodes 0x3-0x7 and 0xB-0xF are reserved for future use.
		return nil, protoErr("reserved opcode %d is not defined", f.OpCode)
	}
}

func (r *Reassembler) handleControl(f Frame) (*Event, error) {
	// FrameParser already rejects fragmented control frames and >125 payloads,
	// but the checks are cheap and keep this type correct when fed directly.
	if !f.Fin {
		return nil, errControlFragment
	}
	if len(f.Payload) > MaxControlPayload {
		return nil, protoErr("control payload of %d bytes exceeds 125", len(f.Payload))
	}
	switch f.OpCode {
	case OpPing:
		return &Event{Type: EventPing, OpCode: OpPing, Payload: f.Payload}, nil
	case OpPong:
		return &Event{Type: EventPong, OpCode: OpPong, Payload: f.Payload}, nil
	case OpClose:
		code := 1005 // RFC 6455 §7.4.1: "no status code received"
		var reason string
		p := f.Payload
		if len(p) == 1 {
			return nil, errClosePayload
		}
		if len(p) >= 2 {
			code = int(p[0])<<8 | int(p[1])
			if !validCloseCode(code) {
				return nil, errClosePayload
			}
			reasonBytes := p[2:]
			if !utf8.Valid(reasonBytes) {
				return nil, errClosePayload
			}
			reason = string(reasonBytes)
		}
		// A close while a fragmented message is in flight ends it; the partial
		// message is simply discarded (connection is closing anyway).
		r.resetFragment()
		return &Event{Type: EventClose, OpCode: OpClose, Payload: f.Payload,
			CloseCode: code, CloseReason: reason}, nil
	default:
		return nil, protoErr("reserved control opcode %d is not defined", f.OpCode)
	}
}

// validCloseCode implements the rules of RFC 6455 §7.4.1/§7.4.2: 1000, 1001,
// 1002, 1003, 1007-1011, and private-use codes 3000-4999 are valid on the wire.
// 1004, 1005, 1006, 1015 and other gaps/reserved ranges are never sent.
func validCloseCode(code int) bool {
	switch code {
	case 1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011:
		return true
	}
	return code >= 3000 && code <= 4999
}

func (r *Reassembler) resetFragment() {
	r.fragActive = false
	r.fragBinary = false
	r.fragValid = false
	r.msgBuf = nil
}
