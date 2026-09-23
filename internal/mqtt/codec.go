// Package mqtt implements the wire-level encode/decode for the MQTT 3.1.1
// subset understood by this server.
//
// Supported control packets:
//
//	CONNECT / CONNACK, PUBLISH (QoS 0 and 1 only), PUBACK,
//	SUBSCRIBE / SUBACK, UNSUBSCRIBE / UNSUBACK,
//	PINGREQ / PINGRESP, DISCONNECT.
//
// Anything else (PUBREC/PUBREL/PUBCOMP i.e. QoS 2, AUTH, reserved types,
// or a second CONNECT on an open connection) is treated as a protocol
// violation: the server closes the network connection without any further
// packet (MQTT-4.8.0-2 style error handling).
package mqtt

import (
	"bufio"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"unicode/utf8"
)

// Control packet type values, high nibble of byte 1.
const (
	TypeRESERVED1   byte = 0
	TypeCONNECT     byte = 1
	TypeCONNACK     byte = 2
	TypePUBLISH     byte = 3
	TypePUBACK      byte = 4
	TypePUBREC      byte = 5
	TypePUBREL      byte = 6
	TypePUBCOMP     byte = 7
	TypeSUBSCRIBE   byte = 8
	TypeSUBACK      byte = 9
	TypeUNSUBSCRIBE byte = 10
	TypeUNSUBACK    byte = 11
	TypePINGREQ     byte = 12
	TypePINGRESP    byte = 13
	TypeDISCONNECT  byte = 14
	TypeRESERVED15  byte = 15
)

// MaxRemainingLength is the MQTT 3.1.1 hard maximum (28-1 encoded).
const MaxRemainingLength = 268435455

// Protocol errors. The broker maps every one of these to "close the
// connection", per the subset's error-handling rule.
var (
	ErrProtocol     = errors.New("mqtt: protocol violation")
	ErrPacketTooBig = errors.New("mqtt: remaining length exceeds maximum")
	ErrTruncated    = errors.New("mqtt: truncated packet")
	ErrReservedType = errors.New("mqtt: reserved control packet type")
)

// Frame is one fully read MQTT control packet: the fixed-header first byte
// plus the decoded remaining-length body (variable header + payload).
type Frame struct {
	// First is fixed header byte 1: type<<4 | flags.
	First byte
	// Body is the exact remaining-length payload.
	Body []byte
}

// Type returns the control packet type (0..15).
func (f *Frame) Type() byte { return f.First >> 4 }

// Flags returns the low 4 flag bits of fixed header byte 1.
func (f *Frame) Flags() byte { return f.First & 0x0F }

// PutString appends an MQTT UTF-8 string (2-byte big-endian length prefix).
func PutString(b []byte, s string) []byte {
	b = binary.BigEndian.AppendUint16(b, uint16(len(s)))
	return append(b, s...)
}

// PutBytes appends an MQTT binary field (same framing as a string).
func PutBytes(b []byte, p []byte) []byte {
	b = binary.BigEndian.AppendUint16(b, uint16(len(p)))
	return append(b, p...)
}

// EncodeRemainingLength appends the variable-length integer encoding.
func EncodeRemainingLength(n int) []byte {
	var out []byte
	for {
		d := byte(n % 128)
		n /= 128
		if n > 0 {
			d |= 0x80
		}
		out = append(out, d)
		if n == 0 {
			return out
		}
	}
}

// DecodeRemainingLength reads a variable-length integer from r, which must
// implement io.ByteReader (bufio.Reader does).
func DecodeRemainingLength(r io.ByteReader) (int, error) {
	multiplier := 1
	value := 0
	for i := 0; i < 4; i++ {
		b, err := r.ReadByte()
		if err != nil {
			return 0, err
		}
		value += int(b&0x7F) * multiplier
		if b&0x80 == 0 {
			return value, nil
		}
		multiplier *= 128
	}
	return 0, fmt.Errorf("%w: bad remaining length", ErrProtocol)
}

// ReadFrame reads one whole control packet.
func ReadFrame(r *bufio.Reader) (*Frame, error) {
	first, err := r.ReadByte()
	if err != nil {
		return nil, err // clean EOF / connection drop: caller distinguishes io.EOF
	}
	n, err := DecodeRemainingLength(r)
	if err != nil {
		return nil, err
	}
	if n > MaxRemainingLength {
		return nil, ErrPacketTooBig
	}
	body := make([]byte, n)
	if _, err := io.ReadFull(r, body); err != nil {
		return nil, fmt.Errorf("%w: %v", ErrTruncated, err)
	}
	return &Frame{First: first, Body: body}, nil
}

// EncodeFrame wraps a remaining-length body with fixed header byte first.
func EncodeFrame(first byte, body []byte) []byte {
	out := []byte{first}
	out = append(out, EncodeRemainingLength(len(body))...)
	return append(out, body...)
}

// cursor is a bounds-checked reader over a packet body. Every failed take
// returns ErrProtocol so the broker only needs one error path: close.
type cursor struct {
	b   []byte
	pos int
}

func (c *cursor) byte() (byte, error) {
	if c.pos+1 > len(c.b) {
		return 0, ErrProtocol
	}
	v := c.b[c.pos]
	c.pos++
	return v, nil
}

func (c *cursor) u16() (uint16, error) {
	if c.pos+2 > len(c.b) {
		return 0, ErrProtocol
	}
	v := binary.BigEndian.Uint16(c.b[c.pos:])
	c.pos += 2
	return v, nil
}

func (c *cursor) bytes() ([]byte, error) {
	n, err := c.u16()
	if err != nil {
		return nil, err
	}
	if c.pos+int(n) > len(c.b) {
		return nil, ErrProtocol
	}
	v := c.b[c.pos : c.pos+int(n)]
	c.pos += int(n)
	return v, nil
}

func (c *cursor) string() (string, error) {
	v, err := c.bytes()
	return string(v), err
}

func (c *cursor) rest() []byte {
	v := c.b[c.pos:]
	c.pos = len(c.b)
	return v
}

func (c *cursor) done() error {
	if c.pos != len(c.b) {
		return fmt.Errorf("%w: %d trailing byte(s)", ErrProtocol, len(c.b)-c.pos)
	}
	return nil
}

// ConnectPacket is a parsed CONNECT packet (MQTT 3.1.1 §3.1).
type ConnectPacket struct {
	ClientID     string
	CleanSession bool
	KeepAlive    uint16
	Username     string
	Password     []byte
	HasUsername  bool
	HasPassword  bool

	WillFlag   bool
	WillQoS    byte
	WillRetain bool
	WillTopic  string
	WillMsg    []byte
}

// CONNECT connect flag bits (fixed header byte 8).
const (
	flagUserName   byte = 0x80
	flagPassword   byte = 0x40
	flagWillRetain byte = 0x20
	flagWillQoS    byte = 0x18 // 2-bit field
	flagWillFlag   byte = 0x04
	flagCleanStart byte = 0x02
)

// DecodeConnect parses a CONNECT body. Every structural deviation returns
// ErrProtocol; callers close the connection with no CONNACK. The handful of
// CONNACK return-code cases (bad version, bad identifier) are returned as
// numbered errors via ConnCodeError so the caller can send CONNACK first.
type ConnCodeError struct {
	Code byte
	Msg  string
}

func (e *ConnCodeError) Error() string { return fmt.Sprintf("mqtt connect: %s (rc=%d)", e.Msg, e.Code) }

// CONNACK return codes (MQTT 3.1.1 §3.2.2.3).
const (
	ConnAccepted              byte = 0
	ConnBadProtocolVersion    byte = 1
	ConnIdentifierRejected    byte = 2
	ConnServerUnavailable     byte = 3
	ConnBadUsernameOrPassword byte = 4
	ConnNotAuthorized         byte = 5
)

// DecodeConnect parses the body of a CONNECT packet.
func DecodeConnect(body []byte) (*ConnectPacket, error) {
	c := &cursor{b: body}

	protoName, err := c.string()
	if err != nil {
		return nil, err
	}
	if protoName != "MQTT" {
		return nil, &ConnCodeError{Code: ConnBadProtocolVersion, Msg: "protocol name must be MQTT"}
	}
	level, err := c.byte()
	if err != nil {
		return nil, err
	}
	if level != 4 { // 3.1.1
		return nil, &ConnCodeError{Code: ConnBadProtocolVersion, Msg: "unsupported protocol level"}
	}
	flags, err := c.byte()
	if err != nil {
		return nil, err
	}
	// Reserved bit MUST be zero (MQTT-3.1.2-3).
	if flags&0x01 != 0 {
		return nil, fmt.Errorf("%w: CONNECT reserved bit set", ErrProtocol)
	}
	keepAlive, err := c.u16()
	if err != nil {
		return nil, err
	}

	p := &ConnectPacket{
		CleanSession: flags&flagCleanStart != 0,
		KeepAlive:    keepAlive,
		HasUsername:  flags&flagUserName != 0,
		HasPassword:  flags&flagPassword != 0,
		WillFlag:     flags&flagWillFlag != 0,
		WillRetain:   flags&flagWillRetain != 0,
	}
	p.WillQoS = (flags & flagWillQoS) >> 3
	if p.WillQoS == 3 { // reserved QoS value
		return nil, fmt.Errorf("%w: bad will QoS", ErrProtocol)
	}
	// Will QoS/retain are meaningless without Will Flag (MQTT-3.1.2-12).
	if !p.WillFlag && (p.WillQoS != 0 || p.WillRetain) {
		return nil, fmt.Errorf("%w: will fields set without will flag", ErrProtocol)
	}
	// Password requires username (MQTT-3.1.2-20/22 ordering).
	if p.HasPassword && !p.HasUsername {
		return nil, fmt.Errorf("%w: password without username", ErrProtocol)
	}

	p.ClientID, err = c.string()
	if err != nil {
		return nil, err
	}
	// Empty client id only allowed with clean session (MQTT-3.1.3-7/8).
	if p.ClientID == "" && !p.CleanSession {
		return nil, &ConnCodeError{Code: ConnIdentifierRejected, Msg: "empty client id requires clean session"}
	}
	if len(p.ClientID) > 65535 || !validMQTTUTF8(p.ClientID) {
		return nil, &ConnCodeError{Code: ConnIdentifierRejected, Msg: "invalid client id"}
	}

	if p.WillFlag {
		p.WillTopic, err = c.string()
		if err != nil {
			return nil, err
		}
		if !ValidTopic(p.WillTopic) {
			return nil, fmt.Errorf("%w: bad will topic", ErrProtocol)
		}
		p.WillMsg, err = c.bytes()
		if err != nil {
			return nil, err
		}
	}
	if p.HasUsername {
		p.Username, err = c.string()
		if err != nil {
			return nil, err
		}
	}
	if p.HasPassword {
		p.Password, err = c.bytes()
		if err != nil {
			return nil, err
		}
	}
	if err := c.done(); err != nil {
		return nil, err
	}
	return p, nil
}

// EncodeConnect builds a CONNECT packet (used by the demo client/tests).
func EncodeConnect(p *ConnectPacket) []byte {
	var vh []byte
	vh = PutString(vh, "MQTT")
	vh = append(vh, 4) // level 3.1.1
	var flags byte
	if p.CleanSession {
		flags |= flagCleanStart
	}
	if p.WillFlag {
		flags |= flagWillFlag | (p.WillQoS << 3)
	}
	if p.WillRetain {
		flags |= flagWillRetain
	}
	if p.HasUsername {
		flags |= flagUserName
	}
	if p.HasPassword {
		flags |= flagPassword
	}
	vh = append(vh, flags)
	vh = binary.BigEndian.AppendUint16(vh, p.KeepAlive)

	var payload []byte
	payload = PutString(payload, p.ClientID)
	if p.WillFlag {
		payload = PutString(payload, p.WillTopic)
		payload = PutBytes(payload, p.WillMsg)
	}
	if p.HasUsername {
		payload = PutString(payload, p.Username)
	}
	if p.HasPassword {
		payload = PutBytes(payload, p.Password)
	}

	body := append(vh, payload...)
	return EncodeFrame(TypeCONNECT<<4, body)
}

// EncodeConnack builds a CONNACK packet.
func EncodeConnack(sessionPresent bool, code byte) []byte {
	sp := byte(0)
	if sessionPresent {
		sp = 1
	}
	return EncodeFrame(TypeCONNACK<<4, []byte{sp, code})
}

// PublishPacket is a parsed inbound PUBLISH (§3.3).
type PublishPacket struct {
	Dup      bool
	QoS      byte
	Retain   bool
	Topic    string
	PacketID uint16 // 0 when QoS 0
	Payload  []byte
}

// DecodePublish parses a PUBLISH body given the fixed-header flag bits.
func DecodePublish(flags byte, body []byte) (*PublishPacket, error) {
	p := &PublishPacket{
		Dup:    flags&0x08 != 0,
		QoS:    (flags >> 1) & 0x03,
		Retain: flags&0x01 != 0,
	}
	if p.QoS == 3 {
		return nil, fmt.Errorf("%w: PUBLISH QoS 3 reserved", ErrProtocol)
	}
	c := &cursor{b: body}
	var err error
	p.Topic, err = c.string()
	if err != nil {
		return nil, err
	}
	if !ValidTopic(p.Topic) {
		return nil, fmt.Errorf("%w: bad publish topic", ErrProtocol)
	}
	if p.QoS > 0 {
		p.PacketID, err = c.u16()
		if err != nil {
			return nil, err
		}
		if p.PacketID == 0 {
			return nil, fmt.Errorf("%w: PUBLISH with QoS>0 must carry non-zero packet id", ErrProtocol)
		}
	} else {
		// DUP and non-zero packet id are meaningless for QoS 0.
		if p.Dup {
			return nil, fmt.Errorf("%w: DUP set on QoS 0 PUBLISH", ErrProtocol)
		}
	}
	p.Payload = c.rest()
	return p, nil
}

// EncodePublish builds an outbound PUBLISH packet.
func EncodePublish(p *PublishPacket) []byte {
	first := TypePUBLISH << 4
	if p.Dup {
		first |= 0x08
	}
	first |= (p.QoS & 0x03) << 1
	if p.Retain {
		first |= 0x01
	}
	var body []byte
	body = PutString(body, p.Topic)
	if p.QoS > 0 {
		body = binary.BigEndian.AppendUint16(body, p.PacketID)
	}
	body = append(body, p.Payload...)
	return EncodeFrame(first, body)
}

// EncodeAck encodes PUBACK (type 4). The same 4-byte shape is used for
// PUBREC/PUBREL/PUBCOMP, which this subset refuses but the client helper
// still needs PUBACK.
func EncodeAck(t byte, packetID uint16) []byte {
	var body []byte
	body = binary.BigEndian.AppendUint16(body, packetID)
	first := t << 4
	if t == TypePUBREL {
		first |= 0x02 // PUBREL fixed flags are 0010
	}
	return EncodeFrame(first, body)
}

// EncodePuback is the common case.
func EncodePuback(packetID uint16) []byte { return EncodeAck(TypePUBACK, packetID) }

// DecodeAck pulls the packet id out of a PUBACK-shaped body.
func DecodeAck(body []byte) (uint16, error) {
	c := &cursor{b: body}
	id, err := c.u16()
	if err != nil {
		return 0, err
	}
	if id == 0 {
		return 0, fmt.Errorf("%w: ack packet id must be non-zero", ErrProtocol)
	}
	if err := c.done(); err != nil {
		return 0, err
	}
	return id, nil
}

// Subscription is one (filter, maxQoS) pair in a SUBSCRIBE payload.
type Subscription struct {
	Filter string
	MaxQoS byte
}

// DecodeSubscribe parses SUBSCRIBE (fixed flags MUST be 0010).
func DecodeSubscribe(flags byte, body []byte) (uint16, []Subscription, error) {
	if flags != 0x02 {
		return 0, nil, fmt.Errorf("%w: SUBSCRIBE fixed flags must be 0010", ErrProtocol)
	}
	c := &cursor{b: body}
	pid, err := c.u16()
	if err != nil {
		return 0, nil, err
	}
	if pid == 0 {
		return 0, nil, fmt.Errorf("%w: SUBSCRIBE packet id must be non-zero", ErrProtocol)
	}
	var subs []Subscription
	for c.pos < len(c.b) {
		filter, err := c.string()
		if err != nil {
			return 0, nil, err
		}
		qos, err := c.byte()
		if err != nil {
			return 0, nil, err
		}
		if qos&0xFC != 0 { // only QoS bits 0-1 allowed
			return 0, nil, fmt.Errorf("%w: bad subscribe QoS field", ErrProtocol)
		}
		subs = append(subs, Subscription{Filter: filter, MaxQoS: qos})
	}
	if len(subs) == 0 {
		return 0, nil, fmt.Errorf("%w: SUBSCRIBE must contain at least one filter", ErrProtocol)
	}
	return pid, subs, nil
}

// EncodeSubscribe builds a SUBSCRIBE packet (client helper).
func EncodeSubscribe(packetID uint16, subs []Subscription) []byte {
	var body []byte
	body = binary.BigEndian.AppendUint16(body, packetID)
	for _, s := range subs {
		body = PutString(body, s.Filter)
		body = append(body, s.MaxQoS&0x03)
	}
	return EncodeFrame((TypeSUBSCRIBE<<4)|0x02, body)
}

// EncodeSuback builds the SUBACK payload: packet id + one return code per
// filter. QoS 1 is granted as 1; failure is 0x80.
func EncodeSuback(packetID uint16, codes []byte) []byte {
	var body []byte
	body = binary.BigEndian.AppendUint16(body, packetID)
	body = append(body, codes...)
	return EncodeFrame(TypeSUBACK<<4, body)
}

// DecodeUnsubscribe parses UNSUBSCRIBE (fixed flags MUST be 0010).
func DecodeUnsubscribe(flags byte, body []byte) (uint16, []string, error) {
	if flags != 0x02 {
		return 0, nil, fmt.Errorf("%w: UNSUBSCRIBE fixed flags must be 0010", ErrProtocol)
	}
	c := &cursor{b: body}
	pid, err := c.u16()
	if err != nil {
		return 0, nil, err
	}
	if pid == 0 {
		return 0, nil, fmt.Errorf("%w: UNSUBSCRIBE packet id must be non-zero", ErrProtocol)
	}
	var filters []string
	for c.pos < len(c.b) {
		f, err := c.string()
		if err != nil {
			return 0, nil, err
		}
		filters = append(filters, f)
	}
	if len(filters) == 0 {
		return 0, nil, fmt.Errorf("%w: UNSUBSCRIBE must contain a filter", ErrProtocol)
	}
	return pid, filters, nil
}

// EncodeUnsubscribe builds an UNSUBSCRIBE packet (client helper).
func EncodeUnsubscribe(packetID uint16, filters []string) []byte {
	var body []byte
	body = binary.BigEndian.AppendUint16(body, packetID)
	for _, f := range filters {
		body = PutString(body, f)
	}
	return EncodeFrame((TypeUNSUBSCRIBE<<4)|0x02, body)
}

// EncodeUnsuback builds UNSUBACK.
func EncodeUnsuback(packetID uint16) []byte {
	var body []byte
	body = binary.BigEndian.AppendUint16(body, packetID)
	return EncodeFrame(TypeUNSUBACK<<4, body)
}

// Simple fixed packets.
var (
	PacketPingReq    = EncodeFrame(TypePINGREQ<<4, nil)
	PacketPingResp   = EncodeFrame(TypePINGRESP<<4, nil)
	PacketDisconnect = EncodeFrame(TypeDISCONNECT<<4, nil)
)

// validMQTTUTF8 implements the MQTT §1.5.3 UTF-8 restrictions: valid Unicode
// non-surrogate text without the U+0000 control character.
func validMQTTUTF8(s string) bool {
	if !utf8.ValidString(s) {
		return false
	}
	for _, r := range s {
		if r == 0 {
			return false
		}
		if r >= 0xD800 && r <= 0xDFFF {
			return false
		}
	}
	return true
}
