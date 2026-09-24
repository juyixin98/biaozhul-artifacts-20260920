// Package protocol implements the subset of Modbus/TCP required by this
// project: framing (MBAP header + PDU), function code 0x03 (Read Holding
// Registers) and function code 0x10 (Write Multiple Registers), including
// their exception responses.
//
// References (freely available):
//
//	Modbus Organization, "MODBUS Messaging on TCP/IP Implementation Guide V1.0b"
//	Modbus Organization, "MODBUS Application Protocol Specification V1.1b3"
package protocol

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"
)

const (
	// Function codes supported.
	FuncReadHoldingRegisters   = 0x03
	FuncWriteMultipleRegisters = 0x10

	// MBAPPrefixLen is the fixed MBAP prefix: TransactionID(2)+ProtocolID(2)
	// +Length(2). The Unit Identifier follows as the FIRST byte counted by
	// the Length field (so the often-quoted "7-byte MBAP header" is this
	// 6-byte prefix plus the 1-byte Unit ID).
	MBAPPrefixLen = 6
	// MBAPHeaderLen is the full 7-byte MBAP header (prefix + Unit ID).
	MBAPHeaderLen = 7
	// ProtocolIDModbus is the only Protocol Identifier defined for Modbus/TCP.
	ProtocolIDModbus = 0x0000
	// MaxPDULength: Length field counts UnitID(1)+PDU. A Modbus PDU is at
	// most 253 bytes, hence the Length field must never exceed 254.
	MaxPDULength     = 253
	MaxLengthField   = 254
	MinReadQuantity  = 1
	MaxReadQuantity  = 125 // FC03 response = 5 + 2*n <= 253
	MaxWriteQuantity = 123 // FC10 request = 6 + 2*n <= 253
	// WriteRequestFixedPDU is FC10 request PDU prefix length:
	// FC(1)+addr(2)+qty(2)+bytecount(1); the value blob (2*qty) follows.
	WriteRequestFixedPDU = 6
)

// Modbus exception codes (Application Protocol spec, Table "Exception codes").
const (
	ExcIllegalFunction              = 0x01
	ExcIllegalDataAddress           = 0x02
	ExcIllegalDataValue             = 0x03
	ExcServerDeviceFailure          = 0x04
	ExcGatewayTargetFailedToRespond = 0x0A
	ExcGatewayPathUnavailable       = 0x0B
)

// ExceptionText maps exception codes to their spec names.
var ExceptionText = map[byte]string{
	ExcIllegalFunction:              "Illegal function",
	ExcIllegalDataAddress:           "Illegal data address",
	ExcIllegalDataValue:             "Illegal data value",
	ExcServerDeviceFailure:          "Server device failure",
	ExcGatewayTargetFailedToRespond: "Gateway target device failed to respond",
	ExcGatewayPathUnavailable:       "Gateway path unavailable",
}

var (
	// ErrTruncated means the stream ended before the declared frame fully
	// arrived (connection dropped mid-frame, or a short send). Callers must
	// not attempt to resynchronise: the length of the missing tail is known
	// to the sender only, so the connection is unusable.
	ErrTruncated = errors.New("modbus: stream truncated inside a frame")
	// ErrFrameTooLarge means the Length field exceeds the Modbus maximum.
	ErrFrameTooLarge = errors.New("modbus: length field exceeds 254")
)

// Frame is one Modbus/TCP ADU: MBAP header plus the complete PDU.
type Frame struct {
	TransactionID uint16
	ProtocolID    uint16
	UnitID        byte
	PDU           []byte
}

func (f Frame) FunctionCode() byte { return f.PDU[0] }

// Encode serialises the frame into a newly allocated byte slice.
func (f Frame) Encode() []byte {
	out := make([]byte, MBAPHeaderLen+len(f.PDU))
	binary.BigEndian.PutUint16(out[0:2], f.TransactionID)
	binary.BigEndian.PutUint16(out[2:4], f.ProtocolID)
	binary.BigEndian.PutUint16(out[4:6], uint16(len(f.PDU)+1)) // UnitID + PDU
	out[6] = f.UnitID
	copy(out[7:], f.PDU)
	return out
}

// ReadFrame reads exactly one length-prefixed Modbus/TCP ADU from r.
//
// Because the MBAP Length field declares the size of the remainder of the
// frame, delivery segmentation ("TCP 粘包/分包") is invisible to callers:
// ReadFull reassembles partial segments and successive frames stay
// distinguishable on a shared connection.
func ReadFrame(r io.Reader) (Frame, error) {
	var prefix [MBAPPrefixLen]byte
	if _, err := io.ReadFull(r, prefix[:]); err != nil {
		if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
			// A clean EOF with zero bytes means the peer closed an idle
			// connection (a normal half-close); any partial prefix is a
			// truncated frame. Preserve the distinction for callers.
			if errors.Is(err, io.EOF) {
				return Frame{}, io.EOF
			}
			return Frame{}, ErrTruncated
		}
		return Frame{}, err
	}
	length := binary.BigEndian.Uint16(prefix[4:6])
	if length < 1 {
		return Frame{}, fmt.Errorf("modbus: illegal length field %d (must cover unit id)", length)
	}
	if length > MaxLengthField {
		return Frame{}, fmt.Errorf("%w: got %d", ErrFrameTooLarge, length)
	}
	// Length counts UnitID(1) + PDU; read them in one shot, then split.
	rest := make([]byte, length)
	if _, err := io.ReadFull(r, rest); err != nil {
		if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
			return Frame{}, ErrTruncated
		}
		return Frame{}, err
	}
	return Frame{
		TransactionID: binary.BigEndian.Uint16(prefix[0:2]),
		ProtocolID:    binary.BigEndian.Uint16(prefix[2:4]),
		UnitID:        rest[0],
		PDU:           rest[1:],
	}, nil
}

// ReadHoldingRegistersRequest is FC03.
type ReadHoldingRegistersRequest struct {
	StartAddress uint16
	Quantity     uint16
}

func (r ReadHoldingRegistersRequest) Encode() []byte {
	pdu := make([]byte, 5)
	pdu[0] = FuncReadHoldingRegisters
	binary.BigEndian.PutUint16(pdu[1:3], r.StartAddress)
	binary.BigEndian.PutUint16(pdu[3:5], r.Quantity)
	return pdu
}

// ParseReadHoldingRegistersRequest validates PDU shape and quantity range.
// Address-range validation against the register bank is the store's job and
// surfaces as exception 0x02; purely structural errors surface as 0x03.
func ParseReadHoldingRegistersRequest(pdu []byte) (ReadHoldingRegistersRequest, byte, bool) {
	if len(pdu) != 5 {
		return ReadHoldingRegistersRequest{}, ExcIllegalDataValue, false
	}
	q := binary.BigEndian.Uint16(pdu[3:5])
	if q < MinReadQuantity || q > MaxReadQuantity {
		return ReadHoldingRegistersRequest{}, ExcIllegalDataValue, false
	}
	return ReadHoldingRegistersRequest{
		StartAddress: binary.BigEndian.Uint16(pdu[1:3]),
		Quantity:     q,
	}, 0, true
}

// EncodeReadHoldingRegistersResponse builds a FC03 normal response:
// FC(1) + byte count(1) + 2*n register bytes.
func EncodeReadHoldingRegistersResponse(values []uint16) []byte {
	pdu := make([]byte, 2+2*len(values))
	pdu[0] = FuncReadHoldingRegisters
	pdu[1] = byte(2 * len(values))
	for i, v := range values {
		binary.BigEndian.PutUint16(pdu[2+2*i:4+2*i], v)
	}
	return pdu
}

// DecodeReadHoldingRegistersResponse parses a normal FC03 response.
func DecodeReadHoldingRegistersResponse(pdu []byte) ([]uint16, error) {
	if len(pdu) < 2 {
		return nil, errors.New("modbus: short FC03 response")
	}
	if pdu[0] != FuncReadHoldingRegisters {
		return nil, fmt.Errorf("modbus: unexpected function code 0x%02X", pdu[0])
	}
	bc := int(pdu[1])
	if bc != len(pdu)-2 || bc%2 != 0 {
		return nil, fmt.Errorf("modbus: FC03 byte count %d inconsistent with %d pdu bytes", bc, len(pdu))
	}
	out := make([]uint16, bc/2)
	for i := range out {
		out[i] = binary.BigEndian.Uint16(pdu[2+2*i:])
	}
	return out, nil
}

// WriteMultipleRegistersRequest is FC10.
type WriteMultipleRegistersRequest struct {
	StartAddress uint16
	Values       []uint16
}

func (w WriteMultipleRegistersRequest) Quantity() uint16 { return uint16(len(w.Values)) }

func (w WriteMultipleRegistersRequest) Encode() []byte {
	pdu := make([]byte, WriteRequestFixedPDU+2*len(w.Values))
	pdu[0] = FuncWriteMultipleRegisters
	binary.BigEndian.PutUint16(pdu[1:3], w.StartAddress)
	binary.BigEndian.PutUint16(pdu[3:5], w.Quantity())
	pdu[5] = byte(2 * len(w.Values))
	for i, v := range w.Values {
		binary.BigEndian.PutUint16(pdu[6+2*i:8+2*i], v)
	}
	return pdu
}

// ParseWriteMultipleRegistersRequest validates PDU shape, quantity, byte
// count and register count limits (spec: 1..123 for FC10). Address-range
// validation against the bank is left to the store (exception 0x02).
func ParseWriteMultipleRegistersRequest(pdu []byte) (WriteMultipleRegistersRequest, byte, bool) {
	if len(pdu) < WriteRequestFixedPDU {
		return WriteMultipleRegistersRequest{}, ExcIllegalDataValue, false
	}
	qty := binary.BigEndian.Uint16(pdu[3:5])
	bc := int(pdu[5])
	if qty < 1 || qty > MaxWriteQuantity {
		return WriteMultipleRegistersRequest{}, ExcIllegalDataValue, false
	}
	if bc != 2*int(qty) {
		return WriteMultipleRegistersRequest{}, ExcIllegalDataValue, false
	}
	if len(pdu) != WriteRequestFixedPDU+2*int(qty) {
		return WriteMultipleRegistersRequest{}, ExcIllegalDataValue, false
	}
	values := make([]uint16, qty)
	for i := range values {
		values[i] = binary.BigEndian.Uint16(pdu[6+2*i:])
	}
	return WriteMultipleRegistersRequest{
		StartAddress: binary.BigEndian.Uint16(pdu[1:3]),
		Values:       values,
	}, 0, true
}

// EncodeWriteMultipleRegistersResponse builds the FC10 echo response.
func EncodeWriteMultipleRegistersResponse(startAddress, quantity uint16) []byte {
	pdu := make([]byte, 5)
	pdu[0] = FuncWriteMultipleRegisters
	binary.BigEndian.PutUint16(pdu[1:3], startAddress)
	binary.BigEndian.PutUint16(pdu[3:5], quantity)
	return pdu
}

// ExceptionResponse builds an exception PDU: original function code | 0x80.
func ExceptionResponse(functionCode, exceptionCode byte) []byte {
	return []byte{functionCode | 0x80, exceptionCode}
}

// AsException returns (exceptionCode, true) if pdu is an exception response.
func AsException(pdu []byte) (byte, bool) {
	if len(pdu) >= 2 && pdu[0]&0x80 != 0 {
		return pdu[1], true
	}
	return 0, false
}
