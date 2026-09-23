// Package mqtt implements a server-side subset of MQTT 3.1.1 (OASIS standard).
//
// Supported subset:
//   - CONNECT (MQTT 3.1.1, level 4; Will is explicitly unsupported -> disconnect)
//   - CONNACK (server -> client)
//   - PUBLISH with QoS 0 and QoS 1 (inbound and outbound), DUP flag honored
//   - PUBACK
//   - SUBSCRIBE / SUBACK (granted QoS min(1, requested))
//   - UNSUBSCRIBE / UNSUBACK
//   - PINGREQ / PINGRESP
//   - DISCONNECT
//
// Explicitly unsupported (the server closes the connection, per the spec):
//   - QoS 2 in any form (PUBREC/PUBREL/PUBCOMP, or QoS 2 PUBLISH)
//   - Will message flag set in CONNECT
//   - SUBSCRIBE with QoS 2 is not rejected; the granted QoS is downgraded to 1
//   - any malformed packet or protocol violation closes the transport
//
// The server also ignores the RETAIN flag on inbound PUBLISH (Retained Message
// delivery is not part of this subset); this is documented behavior, not a
// protocol error.
package mqtt

import (
	"encoding/binary"
	"errors"
	"strings"
	"unicode/utf8"
)

// Packet types, MQTT 3.1.1 table 2.1.
const (
	TypeRESERVED    byte = 0
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
)

// CONNACK return codes, MQTT 3.1.1 table 3.1.
const (
	rcAccepted byte = iota
	rcBadProto
	rcBadID
	rcServerUnavail
	rcBadAuth
	rcNotAuthorized
)

// maxPacketLen caps the Remaining Length of a packet body this server will
// allocate (an application-level limit, stricter than the MQTT maximum).
const maxPacketLen = 1 << 20

// specMaxPacketLen is the largest value the MQTT variable-length encoding can
// represent (4 bytes, 0xFF 0xFF 0xFF 0x7F).
const specMaxPacketLen = 268_435_455

// ErrProtocol is returned for MQTT protocol violations (bad topic, bad QoS,
// malformed packet). Callers normally handle these by closing the transport.
var ErrProtocol = errors.New("mqtt: protocol violation")

// Packet is one decoded inbound MQTT control packet.
type Packet struct {
	Type     byte
	Flags    byte // fixed-header low nibble
	Dup      bool
	QoS      byte
	Retain   bool
	Topic    string
	PacketID uint16
	Payload  []byte
	Filters  []SubFilter // SUBSCRIBE only
	Unsub    []string    // UNSUBSCRIBE only

	// CONNECT fields
	ProtoName    string
	ProtoLevel   byte
	CleanStart   bool
	WillFlag     bool
	WillQoS      byte
	WillRetain   bool
	UsernameFlag bool
	PasswordFlag bool
	KeepAlive    uint16
	ClientID     string
	Username     string
	Password     []byte
}

// SubFilter is one topic filter in a SUBSCRIBE packet with its requested QoS.
type SubFilter struct {
	Filter string
	QoS    byte
}

// decodeRemainingLength reads the MQTT variable-length integer (max 4 bytes).
func decodeRemainingLength(r interface {
	ReadByte() (byte, error)
}) (int, error) {
	multiplier := 1
	value := 0
	for i := 0; i < 4; i++ {
		b, err := r.ReadByte()
		if err != nil {
			return 0, err
		}
		value += int(b&0x7f) * multiplier
		if b&0x80 == 0 {
			if value > specMaxPacketLen {
				return 0, ErrProtocol
			}
			return value, nil
		}
		multiplier *= 128
	}
	return 0, ErrProtocol
}

// encodeRemainingLength appends the MQTT variable-length encoding of n to dst.
func encodeRemainingLength(dst []byte, n int) []byte {
	for {
		b := byte(n % 128)
		n /= 128
		if n > 0 {
			b |= 0x80
		}
		dst = append(dst, b)
		if n == 0 {
			return dst
		}
	}
}

func putString(dst []byte, s string) []byte {
	dst = binary.BigEndian.AppendUint16(dst, uint16(len(s)))
	return append(dst, s...)
}

// readPacket reads and decodes one MQTT control packet from br.
func readPacket(br interface {
	ReadByte() (byte, error)
	Read(p []byte) (int, error)
}) (*Packet, error) {
	first, err := br.ReadByte()
	if err != nil {
		return nil, err
	}
	pType := first >> 4
	flags := first & 0x0f

	remLen, err := decodeRemainingLength(br)
	if err != nil {
		return nil, err
	}
	if remLen > maxPacketLen {
		return nil, ErrProtocol
	}
	body := make([]byte, remLen)
	if _, err := readFull(br, body); err != nil {
		return nil, err
	}

	p := &Packet{Type: pType, Flags: flags}
	switch pType {
	case TypeCONNECT:
		if flags != 0 {
			return nil, ErrProtocol
		}
		if err := p.decodeConnect(body); err != nil {
			return nil, err
		}
	case TypePUBLISH:
		if err := p.decodePublish(first, body); err != nil {
			return nil, err
		}
	case TypePUBACK:
		if flags != 0 || len(body) != 2 {
			return nil, ErrProtocol
		}
		p.PacketID = binary.BigEndian.Uint16(body)
	case TypeSUBSCRIBE:
		// Fixed header reserved bits MUST be 0010 (MQTT 3.1.1 §3.8.1).
		if flags != 0x02 {
			return nil, ErrProtocol
		}
		if err := p.decodeSubscribe(body); err != nil {
			return nil, err
		}
	case TypeUNSUBSCRIBE:
		// Reserved bits MUST be 0010 (MQTT 3.1.1 §3.10.1).
		if flags != 0x02 {
			return nil, ErrProtocol
		}
		if err := p.decodeUnsubscribe(body); err != nil {
			return nil, err
		}
	case TypePINGREQ:
		if flags != 0 || remLen != 0 {
			return nil, ErrProtocol
		}
	case TypeDISCONNECT:
		if flags != 0 || remLen != 0 {
			return nil, ErrProtocol
		}
	default:
		// PUBREC(5), PUBREL(6), PUBCOMP(7), SUBACK(9), UNSUBACK(11),
		// PINGRESP(13), RESERVED(0,15) are not valid client -> server packets
		// in this subset.
		return nil, ErrProtocol
	}
	return p, nil
}

func readFull(r interface{ Read(p []byte) (int, error) }, buf []byte) (int, error) {
	total := 0
	for total < len(buf) {
		n, err := r.Read(buf[total:])
		total += n
		if err != nil {
			return total, err
		}
	}
	return total, nil
}

func decodeString(body []byte, pos int) (string, int, error) {
	if pos+2 > len(body) {
		return "", 0, ErrProtocol
	}
	l := int(binary.BigEndian.Uint16(body[pos:]))
	pos += 2
	if pos+l > len(body) {
		return "", 0, ErrProtocol
	}
	return string(body[pos : pos+l]), pos + l, nil
}

func (p *Packet) decodeConnect(body []byte) error {
	var pos int
	var err error
	if p.ProtoName, pos, err = decodeString(body, 0); err != nil {
		return err
	}
	if p.ProtoName != "MQTT" {
		// MQTT 3.1 used "MQIsdp"; this subset speaks 3.1.1 only.
		return badProtoErr{}
	}
	if pos+4 > len(body) {
		return ErrProtocol
	}
	p.ProtoLevel = body[pos]
	connectFlags := body[pos+1]
	p.KeepAlive = binary.BigEndian.Uint16(body[pos+2:])
	pos += 4

	p.CleanStart = connectFlags&0x02 != 0
	p.WillFlag = connectFlags&0x04 != 0
	p.WillQoS = (connectFlags >> 3) & 0x03
	p.WillRetain = connectFlags&0x20 != 0
	p.PasswordFlag = connectFlags&0x40 != 0
	p.UsernameFlag = connectFlags&0x80 != 0

	// Reserved bit of the CONNECT flags MUST be 0.
	if connectFlags&0x01 != 0 {
		return ErrProtocol
	}
	if p.WillFlag {
		// Will QoS 3 is a protocol error; any other Will is a subset
		// limitation handled by the caller (explicitly unsupported).
		if p.WillQoS == 3 {
			return ErrProtocol
		}
	} else {
		if p.WillQoS != 0 || p.WillRetain {
			return ErrProtocol
		}
	}

	if p.ClientID, pos, err = decodeString(body, pos); err != nil {
		return err
	}
	if !validClientID(p.ClientID) {
		return badClientIDErr{}
	}
	// Empty Client ID is only allowed with CleanSession=1 (§3.1.3-6).
	if p.ClientID == "" && !p.CleanStart {
		return badClientIDErr{}
	}

	if p.WillFlag {
		// Will topic/payload must be present structurally; the caller rejects
		// the feature before using them, but malformed framing is still fatal.
		if _, pos, err = decodeString(body, pos); err != nil {
			return err
		}
		if pos+2 > len(body) {
			return ErrProtocol
		}
		pl := int(binary.BigEndian.Uint16(body[pos:]))
		pos += 2
		if pos+pl > len(body) {
			return ErrProtocol
		}
		pos += pl
	}
	if p.UsernameFlag {
		if p.Username, pos, err = decodeString(body, pos); err != nil {
			return err
		}
	}
	if p.PasswordFlag {
		if pos+2 > len(body) {
			return ErrProtocol
		}
		pl := int(binary.BigEndian.Uint16(body[pos:]))
		pos += 2
		if pos+pl > len(body) {
			return ErrProtocol
		}
		p.Password = body[pos : pos+pl]
		pos += pl
	}
	if pos != len(body) {
		return ErrProtocol
	}
	return nil
}

// badProtoErr marks an unsupported protocol Name/Version (CONNACK rc=1).
type badProtoErr struct{}

func (badProtoErr) Error() string { return "mqtt: unacceptable protocol version" }

// badClientIDErr marks an identifier rejected by the server (CONNACK rc=2).
type badClientIDErr struct{}

func (badClientIDErr) Error() string { return "mqtt: client identifier rejected" }

// validClientID applies MQTT 3.1.1 §3.1.3.1: the identifier is a UTF-8
// encoded string of 1..65535 characters (an empty identifier is allowed only
// with CleanSession=1, checked by the caller). The legacy 23-character /
// alphanumeric restriction of MQTT 3.1 is not enforced. NUL is rejected
// because it is unsafe in a length-prefixed but otherwise text identifier.
func validClientID(id string) bool {
	if len(id) > 65535 {
		return false
	}
	if !utf8.ValidString(id) {
		return false
	}
	return !strings.ContainsRune(id, 0)
}

func (p *Packet) decodePublish(first byte, body []byte) error {
	p.Dup = first&0x08 != 0
	p.QoS = (first >> 1) & 0x03
	p.Retain = first&0x01 != 0
	if p.QoS == 3 {
		return ErrProtocol
	}
	topic, pos, err := decodeString(body, 0)
	if err != nil {
		return err
	}
	if !validPublishTopic(topic) {
		return ErrProtocol
	}
	p.Topic = topic
	if p.QoS > 0 {
		if pos+2 > len(body) {
			return ErrProtocol
		}
		p.PacketID = binary.BigEndian.Uint16(body[pos:])
		if p.PacketID == 0 {
			return ErrProtocol
		}
		pos += 2
	} else {
		if p.Dup {
			// DUP MUST be 0 for QoS 0 PUBLISH (§3.3.1.1).
			return ErrProtocol
		}
	}
	p.Payload = body[pos:]
	return nil
}

func (p *Packet) decodeSubscribe(body []byte) error {
	if len(body) < 3 {
		return ErrProtocol
	}
	pid := binary.BigEndian.Uint16(body[:2])
	if pid == 0 {
		return ErrProtocol
	}
	p.PacketID = pid
	pos := 2
	for pos < len(body) {
		f, np, err := decodeString(body, pos)
		if err != nil {
			return err
		}
		pos = np
		if pos >= len(body) {
			return ErrProtocol
		}
		reqQoS := body[pos]
		pos++
		if reqQoS > 2 {
			// Failure return code 0x80 for that filter instead of a fatal
			// protocol error (§3.8.3.1 allows 0x00/0x01/0x02/0x80).
			reqQoS = 0x80
		}
		p.Filters = append(p.Filters, SubFilter{Filter: f, QoS: reqQoS})
	}
	if len(p.Filters) == 0 {
		return ErrProtocol // SUBSCRIBE MUST contain at least one filter
	}
	return nil
}

func (p *Packet) decodeUnsubscribe(body []byte) error {
	if len(body) < 2 {
		return ErrProtocol
	}
	pid := binary.BigEndian.Uint16(body[:2])
	if pid == 0 {
		return ErrProtocol
	}
	p.PacketID = pid
	pos := 2
	for pos < len(body) {
		f, np, err := decodeString(body, pos)
		if err != nil {
			return err
		}
		pos = np
		p.Unsub = append(p.Unsub, f)
	}
	if len(p.Unsub) == 0 || pos != len(body) {
		return ErrProtocol
	}
	return nil
}

// validPublishTopic checks the MQTT topic name charset rules (§4.7).
func validPublishTopic(topic string) bool {
	if len(topic) == 0 || len(topic) > 65535 {
		return false
	}
	for _, r := range topic {
		if r == 0 || r == '+' || r == '#' {
			return false
		}
	}
	return true
}

// validFilter checks a topic filter used in SUBSCRIBE (§4.7.1).
func validFilter(f string) bool {
	if len(f) == 0 || len(f) > 65535 {
		return false
	}
	prev := rune(0)
	for i, r := range f {
		switch r {
		case 0:
			return false
		case '+':
			// '+' must occupy a whole level.
			if prev != 0 && prev != '/' {
				return false
			}
			if i+1 < len(f) && f[i+1] != '/' {
				return false
			}
		case '#':
			if prev != 0 && prev != '/' {
				return false
			}
			if i != len(f)-1 {
				return false
			}
		}
		prev = r
	}
	return true
}

// topicMatch reports whether filter matches topic (MQTT 3.1.1 §4.7).
// Validated earlier by validFilter/validPublishTopic, so both are non-empty.
func topicMatch(filter, topic string) bool {
	for {
		fLevel, fRest := splitLevel(filter)
		tLevel, tRest := splitLevel(topic)
		if fLevel == "#" {
			// '#' matches its parent level and every level below:
			// "a/#" matches "a" and "a/b" (non-normative §4.7.1.2 table).
			return true
		}
		if topic == "" {
			// Topic exhausted; any remaining filter level fails.
			return false
		}
		if fLevel != "+" && fLevel != tLevel {
			return false
		}
		filter, topic = fRest, tRest
		if filter == "" || topic == "" {
			// Exact exhaustion matches; a trailing '#' on the filter still
			// matches the just-consumed parent level ("a/#" matches "a").
			return filter == topic || filter == "#"
		}
	}
}

// splitLevel splits off the first topic level (up to the next '/').
func splitLevel(s string) (level, rest string) {
	if i := strings.IndexByte(s, '/'); i >= 0 {
		return s[:i], s[i+1:]
	}
	return s, ""
}

// ---- encoders (server -> client) ----

func connackPacket(sessionPresent bool, code byte) []byte {
	sp := byte(0)
	if sessionPresent {
		sp = 1
	}
	return []byte{0x20, 0x02, sp, code}
}

// publishPacket encodes a PUBLISH packet. qoS must be 0 or 1; for QoS 1 pid
// must be non-zero. When dup is true the QoS 1 redelivery rules apply.
func publishPacket(dup bool, qoS byte, retain bool, topic string, pid uint16, payload []byte) []byte {
	var first byte = TypePUBLISH << 4
	if dup {
		first |= 0x08
	}
	first |= (qoS & 0x03) << 1
	if retain {
		first |= 0x01
	}
	var varHead []byte
	varHead = putString(varHead, topic)
	if qoS > 0 {
		varHead = binary.BigEndian.AppendUint16(varHead, pid)
	}
	rem := len(varHead) + len(payload)
	out := []byte{first}
	out = encodeRemainingLength(out, rem)
	out = append(out, varHead...)
	out = append(out, payload...)
	return out
}

func pubackPacket(pid uint16) []byte {
	return []byte{TypePUBACK << 4, 0x02, byte(pid >> 8), byte(pid)}
}

func subackPacket(pid uint16, codes []byte) []byte {
	out := []byte{TypeSUBACK << 4}
	out = encodeRemainingLength(out, 2+len(codes))
	out = binary.BigEndian.AppendUint16(out, pid)
	out = append(out, codes...)
	return out
}

func unsubackPacket(pid uint16) []byte {
	return []byte{TypeUNSUBACK << 4, 0x02, byte(pid >> 8), byte(pid)}
}

func pingrespPacket() []byte { return []byte{TypePINGRESP << 4, 0x00} }
