// Package protocol implements Modbus TCP framing (MBAP + PDU) for the
// function codes used by this project:
//
//   - 0x03 Read Holding Registers
//   - 0x10 Write Multiple Registers
//
// The reader is a streaming framer: it first reads the fixed 7-byte MBAP
// header, then uses the MBAP Length field to read the exact PDU size. This
// means arbitrary TCP segmentation (one byte per segment) and coalescing
// (multiple ADUs in one TCP segment) are handled correctly.
package protocol

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"
)

// Modbus TCP constants (MODBUS Messaging on TCP/IP, V1.0b).
const (
	MBAPHeaderSize = 7
	ProtocolIDTCP  = 0
	MinPDUSize     = 2 // function code + at least one byte of data / exception code

	// Function codes supported by this project.
	FCReadHoldingRegisters   = 0x03
	FCWriteMultipleRegisters = 0x10
)

// Modbus exception codes (MODBUS Application Protocol V1.1b3, Table 7).
const (
	ExcIllegalFunction                    = 0x01
	ExcIllegalDataAddress                 = 0x02
	ExcIllegalDataValue                   = 0x03
	ExcSlaveDeviceFailure                 = 0x04
	ExcGatewayTargetDeviceFailedToRespond = 0x0B
)

// ExceptionText maps exception codes to the names used by MODBUS V1.1b3.
var ExceptionText = map[byte]string{
	ExcIllegalFunction:                    "ILLEGAL FUNCTION",
	ExcIllegalDataAddress:                 "ILLEGAL DATA ADDRESS",
	ExcIllegalDataValue:                   "ILLEGAL DATA VALUE",
	ExcSlaveDeviceFailure:                 "SLAVE DEVICE FAILURE",
	ExcGatewayTargetDeviceFailedToRespond: "GATEWAY TARGET DEVICE FAILED TO RESPOND",
}

// Limits from MODBUS Application Protocol V1.1b3, Section 6.
const (
	MaxReadQuantity  = 125 // FC03: 1..125 registers
	MaxWriteQuantity = 123 // FC10: 1..123 registers
)

// FrameError describes a malformed MBAP/PDU framing error. Per MODBUS TCP
// specification, a frame whose Length/Protocol fields are inconsistent is
// not a valid request and cannot be answered (there is no trustworthy
// transaction id); the receiver must discard the connection.
type FrameError struct {
	Reason string
}

func (e *FrameError) Error() string { return "modbus frame error: " + e.Reason }

func frameError(format string, a ...any) error {
	return &FrameError{Reason: fmt.Sprintf(format, a...)}
}

// ADU is one Modbus TCP Application Data Unit.
type ADU struct {
	TransactionID uint16
	ProtocolID    uint16
	UnitID        byte
	PDU           []byte // function code + data
}

// Marshal serializes the ADU, recomputing the MBAP length field from the PDU.
func (a *ADU) Marshal() ([]byte, error) {
	if len(a.PDU) < MinPDUSize {
		return nil, fmt.Errorf("PDU too short: %d bytes", len(a.PDU))
	}
	if len(a.PDU)+1 > 0xFFFF { // +1 for Unit ID
		return nil, fmt.Errorf("PDU too long: %d bytes", len(a.PDU))
	}
	buf := make([]byte, MBAPHeaderSize+len(a.PDU))
	binary.BigEndian.PutUint16(buf[0:2], a.TransactionID)
	binary.BigEndian.PutUint16(buf[2:4], a.ProtocolID)
	binary.BigEndian.PutUint16(buf[4:6], uint16(len(a.PDU)+1))
	buf[6] = a.UnitID
	copy(buf[7:], a.PDU)
	return buf, nil
}

// ReadADU reads exactly one framed ADU from r.
//
// It returns io.EOF only when no bytes at all were read before the peer
// closed. A partial frame followed by EOF returns io.ErrUnexpectedEOF —
// the caller logs it and drops the connection, which is the specified
// behavior for a truncated request.
func ReadADU(r io.Reader) (*ADU, error) {
	header := make([]byte, MBAPHeaderSize)
	if _, err := io.ReadFull(r, header); err != nil {
		if errors.Is(err, io.EOF) {
			return nil, io.EOF // clean close between ADUs
		}
		return nil, err // io.ErrUnexpectedEOF or I/O error: half a header
	}

	adu := &ADU{
		TransactionID: binary.BigEndian.Uint16(header[0:2]),
		ProtocolID:    binary.BigEndian.Uint16(header[2:4]),
		UnitID:        header[6],
	}
	length := binary.BigEndian.Uint16(header[4:6])

	// The Length field counts Unit ID + PDU and must be at least 1+1.
	if length < 1+MinPDUSize {
		return nil, frameError("length field %d smaller than minimum %d", length, 1+MinPDUSize)
	}
	if adu.ProtocolID != ProtocolIDTCP {
		return nil, frameError("protocol id %d is not Modbus TCP (0)", adu.ProtocolID)
	}

	pdu := make([]byte, length-1)
	if _, err := io.ReadFull(r, pdu); err != nil {
		// A complete MBAP header followed by EOF before any PDU byte is a
		// truncated frame too, not a clean connection close.
		if errors.Is(err, io.EOF) {
			err = io.ErrUnexpectedEOF
		}
		return nil, fmt.Errorf("reading PDU of %d bytes: %w", len(pdu), err)
	}
	adu.PDU = pdu
	return adu, nil
}

// ReadRequest decodes FC03/FC10 request PDU contents. It returns a
// *RequestError for semantically invalid but well-framed requests; the
// server converts those into Modbus exception responses (the connection
// stays open). Returns a *FrameError only for PDU structural problems
// that corrupt the stream — for FC10 a wrong byte count means the frame
// boundaries cannot be trusted on this connection, so the server closes it.
type Request struct {
	Function byte
	Address  uint16
	Quantity uint16
	Values   []uint16 // only for FC10
}

// RequestError is a decodable request that violates Modbus request rules.
type RequestError struct {
	Function byte // may be 0 if not even a function code is available
	Code     byte // Modbus exception code
	Reason   string
	// Fatal means the connection must be closed rather than answering
	// (declared/actual byte-count mismatch in FC10 desynchronizes framing
	// only when the TCP stream contained extra bytes that belong to the
	// next message; here framing is MBAP-driven, so this is informational).
	Fatal bool
}

func (e *RequestError) Error() string {
	return fmt.Sprintf("modbus request error (fc=0x%02x exc=0x%02x %s): %s",
		e.Function, e.Code, ExceptionText[e.Code], e.Reason)
}

// DecodeRequest parses an FC03/FC10 request. Unknown function codes produce
// an ILLEGAL FUNCTION exception.
func DecodeRequest(pdu []byte) (*Request, error) {
	if len(pdu) < MinPDUSize {
		return nil, frameError("PDU shorter than %d bytes", MinPDUSize)
	}
	fc := pdu[0]
	switch fc {
	case FCReadHoldingRegisters:
		if len(pdu) != 5 {
			return nil, &RequestError{
				Function: fc, Code: ExcIllegalDataValue,
				Reason: fmt.Sprintf("FC03 request must be 5 bytes, got %d", len(pdu)),
			}
		}
		q := binary.BigEndian.Uint16(pdu[3:5])
		if q < 1 || q > MaxReadQuantity {
			return nil, &RequestError{
				Function: fc, Code: ExcIllegalDataValue,
				Reason: fmt.Sprintf("FC03 quantity %d outside 1..%d", q, MaxReadQuantity),
			}
		}
		return &Request{
			Function: fc,
			Address:  binary.BigEndian.Uint16(pdu[1:3]),
			Quantity: q,
		}, nil

	case FCWriteMultipleRegisters:
		if len(pdu) < 6 {
			return nil, &RequestError{
				Function: fc, Code: ExcIllegalDataValue,
				Reason: fmt.Sprintf("FC10 request too short: %d bytes", len(pdu)),
			}
		}
		addr := binary.BigEndian.Uint16(pdu[1:3])
		q := binary.BigEndian.Uint16(pdu[3:5])
		byteCount := pdu[5]
		if q < 1 || q > MaxWriteQuantity {
			return nil, &RequestError{
				Function: fc, Code: ExcIllegalDataValue,
				Reason: fmt.Sprintf("FC10 quantity %d outside 1..%d", q, MaxWriteQuantity),
			}
		}
		if int(byteCount) != int(q)*2 {
			return nil, &RequestError{
				Function: fc, Code: ExcIllegalDataValue,
				Reason: fmt.Sprintf("FC10 byte count %d does not match quantity %d (want %d)",
					byteCount, q, q*2),
			}
		}
		if len(pdu) != 6+int(byteCount) {
			// MBAP said this was the whole PDU, so this is a malformed
			// request; answering is safe because framing is length-driven.
			return nil, &RequestError{
				Function: fc, Code: ExcIllegalDataValue,
				Reason: fmt.Sprintf("FC10 PDU length %d inconsistent with byte count %d",
					len(pdu), byteCount),
			}
		}
		values := make([]uint16, q)
		for i := uint16(0); i < q; i++ {
			values[i] = binary.BigEndian.Uint16(pdu[6+2*i : 8+2*i])
		}
		return &Request{Function: fc, Address: addr, Quantity: q, Values: values}, nil

	default:
		return nil, &RequestError{
			Function: fc, Code: ExcIllegalFunction,
			Reason: fmt.Sprintf("function code 0x%02x not supported", fc),
		}
	}
}

// ExceptionPDU builds an exception response PDU: FC | 0x80 + exception code.
func ExceptionPDU(fc, code byte) []byte {
	return []byte{fc | 0x80, code}
}

// ReadResponsePDU builds an FC03 normal response: FC | byte count | values.
func ReadResponsePDU(values []uint16) []byte {
	pdu := make([]byte, 2+2*len(values))
	pdu[0] = FCReadHoldingRegisters
	pdu[1] = byte(2 * len(values))
	for i, v := range values {
		binary.BigEndian.PutUint16(pdu[2+2*i:], v)
	}
	return pdu
}

// WriteResponsePDU builds an FC10 echo response: FC | address | quantity.
func WriteResponsePDU(addr, quantity uint16) []byte {
	pdu := make([]byte, 5)
	pdu[0] = FCWriteMultipleRegisters
	binary.BigEndian.PutUint16(pdu[1:3], addr)
	binary.BigEndian.PutUint16(pdu[3:5], quantity)
	return pdu
}

// ParseReadResponse decodes an FC03 response. A non-nil error means the
// response is malformed or an exception (use AsException to inspect it).
func ParseReadResponse(pdu []byte) ([]uint16, error) {
	if len(pdu) < 2 {
		return nil, fmt.Errorf("FC03 response too short: %d bytes", len(pdu))
	}
	if pdu[0]&0x80 != 0 {
		return nil, parseException(pdu)
	}
	if pdu[0] != FCReadHoldingRegisters {
		return nil, fmt.Errorf("expected FC03 response, got 0x%02x", pdu[0])
	}
	bc := int(pdu[1])
	if bc != len(pdu)-2 || bc%2 != 0 {
		return nil, fmt.Errorf("FC03 byte count %d inconsistent with PDU length %d", bc, len(pdu))
	}
	values := make([]uint16, bc/2)
	for i := range values {
		values[i] = binary.BigEndian.Uint16(pdu[2+2*i:])
	}
	return values, nil
}

// ParseWriteResponse validates an FC10 echo response.
func ParseWriteResponse(pdu []byte, addr, quantity uint16) error {
	if len(pdu) < 2 {
		return fmt.Errorf("FC10 response too short: %d bytes", len(pdu))
	}
	if pdu[0]&0x80 != 0 {
		return parseException(pdu)
	}
	if pdu[0] != FCWriteMultipleRegisters || len(pdu) != 5 {
		return fmt.Errorf("malformed FC10 response: % x", pdu)
	}
	if binary.BigEndian.Uint16(pdu[1:3]) != addr || binary.BigEndian.Uint16(pdu[3:5]) != quantity {
		return fmt.Errorf("FC10 echo mismatch: addr=%d qty=%d",
			binary.BigEndian.Uint16(pdu[1:3]), binary.BigEndian.Uint16(pdu[3:5]))
	}
	return nil
}

// Exception is a decoded Modbus exception response.
type Exception struct {
	Function byte // original function code (high bit cleared)
	Code     byte
}

func (e *Exception) Error() string {
	name := ExceptionText[e.Code]
	if name == "" {
		name = "UNKNOWN EXCEPTION"
	}
	return fmt.Sprintf("modbus exception 0x%02x (%s) on FC 0x%02x", e.Code, name, e.Function)
}

func parseException(pdu []byte) error {
	if len(pdu) < 2 {
		return fmt.Errorf("exception response truncated")
	}
	return &Exception{Function: pdu[0] & 0x7f, Code: pdu[1]}
}

// AsException extracts a *Exception from an error returned by the parsers.
func AsException(err error) (*Exception, bool) {
	var e *Exception
	if errors.As(err, &e) {
		return e, true
	}
	return nil, false
}

// ReadRequestPDU / WriteRequestPDU construct request PDUs for the client.
func ReadRequestPDU(addr, quantity uint16) []byte {
	pdu := make([]byte, 5)
	pdu[0] = FCReadHoldingRegisters
	binary.BigEndian.PutUint16(pdu[1:3], addr)
	binary.BigEndian.PutUint16(pdu[3:5], quantity)
	return pdu
}

// WriteRequestPDU builds an FC10 request PDU from register values.
func WriteRequestPDU(addr uint16, values []uint16) []byte {
	pdu := make([]byte, 6+2*len(values))
	pdu[0] = FCWriteMultipleRegisters
	binary.BigEndian.PutUint16(pdu[1:3], addr)
	binary.BigEndian.PutUint16(pdu[3:5], uint16(len(values)))
	pdu[5] = byte(2 * len(values))
	for i, v := range values {
		binary.BigEndian.PutUint16(pdu[6+2*i:], v)
	}
	return pdu
}
