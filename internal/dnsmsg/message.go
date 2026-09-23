// Package dnsmsg 实现 DNS 报文（RFC 1035）的构建与安全解析。
//
// 解析重点在名称压缩指针的安全性：指针越界、前向/自指指针（可构造环）、
// 保留标签类型、名称过长以及报文截断都会被显式拒绝，不会出现无限跳转。
package dnsmsg

import (
	"encoding/binary"
	"errors"
	"net"
	"strings"
)

const (
	TypeA    uint16 = 1
	TypeNS   uint16 = 2
	TypeAAAA uint16 = 28
	TypeOPT  uint16 = 41 // EDNS（在 additional 段出现，解析时按普通 RR 处理）

	ClassIN uint16 = 1

	headerLen       = 12
	maxNameWireLen  = 255
	maxLabelLen     = 63
	maxNameJumps    = 64 // 压缩指针链最大跳转次数（纵深防御）
	maxSectionCount = 4096
)

// 解析/构建过程中可能返回的错误。
var (
	ErrShortMessage       = errors.New("dnsmsg: message too short (truncated)")
	ErrBadRDLength        = errors.New("dnsmsg: RDLENGTH exceeds message bounds")
	ErrTooManyRecords     = errors.New("dnsmsg: section count exceeds safety limit")
	ErrReservedLabelType  = errors.New("dnsmsg: reserved label type (10/01 prefix)")
	ErrPointerOutOfBounds = errors.New("dnsmsg: compression pointer out of bounds")
	ErrPointerLoop        = errors.New("dnsmsg: compression pointer loop")
	ErrForwardPointer     = errors.New("dnsmsg: forward/self compression pointer")
	ErrNameTooLong        = errors.New("dnsmsg: domain name exceeds 255 octets")
	ErrLabelTooLong       = errors.New("dnsmsg: label exceeds 63 octets")
	ErrInvalidName        = errors.New("dnsmsg: invalid domain name")
	ErrInvalidQType       = errors.New("dnsmsg: only A/AAAA queries are supported")
)

// Question 是 DNS 问题段中的一个问题。
type Question struct {
	Name  string
	Type  uint16
	Class uint16
}

// Record 是一个资源记录。A/AAAA 的地址同时解析到 IP 字段。
type Record struct {
	Name  string
	Type  uint16
	Class uint16
	TTL   uint32
	Data  []byte
	IP    net.IP
}

// Message 是解析后的 DNS 报文。
type Message struct {
	Raw []byte

	ID     uint16
	QR     bool
	OpCode byte
	AA     bool
	TC     bool
	RD     bool
	RA     bool
	RCode  byte

	Questions []Question
	Answers   []Record
	Authority []Record
	Extra     []Record
}

// IsResponse 报告该报文 QR 位是否置位。
func (m *Message) IsResponse() bool { return m.QR }

// CanonicalName 把名称规范化为小写、带根点的形式（example.com. / .）。
func CanonicalName(name string) string {
	if name == "" {
		return "."
	}
	if !strings.HasSuffix(name, ".") {
		name += "."
	}
	return strings.ToLower(name)
}

// ParseMessage 安全解析一个完整的 DNS 报文。
func ParseMessage(msg []byte) (*Message, error) {
	if len(msg) < headerLen {
		return nil, ErrShortMessage
	}

	m := &Message{Raw: msg}
	m.ID = binary.BigEndian.Uint16(msg[0:2])
	flags := binary.BigEndian.Uint16(msg[2:4])
	m.QR = flags&0x8000 != 0
	m.OpCode = byte((flags >> 11) & 0x0F)
	m.AA = flags&0x0400 != 0
	m.TC = flags&0x0200 != 0
	m.RD = flags&0x0100 != 0
	m.RA = flags&0x0080 != 0
	m.RCode = byte(flags & 0x000F)

	qd := int(binary.BigEndian.Uint16(msg[4:6]))
	an := int(binary.BigEndian.Uint16(msg[6:8]))
	ns := int(binary.BigEndian.Uint16(msg[8:10]))
	ar := int(binary.BigEndian.Uint16(msg[10:12]))
	if qd > maxSectionCount || an > maxSectionCount ||
		ns > maxSectionCount || ar > maxSectionCount {
		return nil, ErrTooManyRecords
	}

	pos := headerLen
	var err error
	if m.Questions, pos, err = parseQuestions(msg, pos, qd); err != nil {
		return nil, err
	}
	if m.Answers, pos, err = parseRecords(msg, pos, an); err != nil {
		return nil, err
	}
	if m.Authority, pos, err = parseRecords(msg, pos, ns); err != nil {
		return nil, err
	}
	if m.Extra, pos, err = parseRecords(msg, pos, ar); err != nil {
		return nil, err
	}
	return m, nil
}

func parseQuestions(msg []byte, pos, count int) ([]Question, int, error) {
	questions := make([]Question, 0, count)
	for i := 0; i < count; i++ {
		name, next, err := decodeName(msg, pos)
		if err != nil {
			return nil, 0, err
		}
		if next+4 > len(msg) {
			return nil, 0, ErrShortMessage
		}
		questions = append(questions, Question{
			Name:  name,
			Type:  binary.BigEndian.Uint16(msg[next : next+2]),
			Class: binary.BigEndian.Uint16(msg[next+2 : next+4]),
		})
		pos = next + 4
	}
	return questions, pos, nil
}

func parseRecords(msg []byte, pos, count int) ([]Record, int, error) {
	records := make([]Record, 0, count)
	for i := 0; i < count; i++ {
		name, next, err := decodeName(msg, pos)
		if err != nil {
			return nil, 0, err
		}
		// 固定部分：TYPE(2) CLASS(2) TTL(4) RDLENGTH(2)
		if next+10 > len(msg) {
			return nil, 0, ErrShortMessage
		}
		rr := Record{
			Name:  name,
			Type:  binary.BigEndian.Uint16(msg[next : next+2]),
			Class: binary.BigEndian.Uint16(msg[next+2 : next+4]),
			TTL:   binary.BigEndian.Uint32(msg[next+4 : next+8]),
		}
		rdlength := int(binary.BigEndian.Uint16(msg[next+8 : next+10]))
		start := next + 10
		if start+rdlength > len(msg) {
			return nil, 0, ErrBadRDLength
		}
		rr.Data = msg[start : start+rdlength]
		switch rr.Type {
		case TypeA:
			if rdlength == 4 {
				ip := make(net.IP, 4)
				copy(ip, rr.Data)
				rr.IP = ip
			}
		case TypeAAAA:
			if rdlength == 16 {
				ip := make(net.IP, 16)
				copy(ip, rr.Data)
				rr.IP = ip
			}
		}
		records = append(records, rr)
		pos = start + rdlength
	}
	return records, pos, nil
}

// decodeName 从 offset 处解析一个（可能含压缩指针的）域名。
// 返回规范化名称（小写、带根点），以及问题/RR 固定字段的起始偏移：
// 未发生跳转时是根零字节之后；发生跳转时是首个指针的两个字节之后。
func decodeName(msg []byte, offset int) (string, int, error) {
	if offset < 0 || offset >= len(msg) {
		return "", 0, ErrPointerOutOfBounds
	}

	var b strings.Builder
	wireLen := 0
	pos := offset
	nextOffset := 0
	jumped := false
	jumps := 0

	for {
		if pos >= len(msg) {
			return "", 0, ErrShortMessage
		}
		length := int(msg[pos])

		switch {
		case length == 0: // 根标签，名称结束
			pos++
			if !jumped {
				nextOffset = pos
			}
			if b.Len() == 0 {
				return ".", nextOffset, nil
			}
			return b.String() + ".", nextOffset, nil

		case length&0xC0 == 0xC0: // 压缩指针 11xxxxxx
			if pos+1 >= len(msg) {
				return "", 0, ErrShortMessage
			}
			target := int(binary.BigEndian.Uint16(msg[pos:pos+2]) & 0x3FFF)
			// 先判越界：指向报文之外（含 0xFFFF 之类）或 12 字节头部。
			if target < headerLen || target >= len(msg) {
				return "", 0, ErrPointerOutOfBounds
			}
			// RFC 1035 4.1.4：指针只能指向报文中“先前出现”的位置。
			// 自指即单节点环；指向更靠后的位置为前向指针（互指环必含此类）。
			if target == pos {
				return "", 0, ErrPointerLoop
			}
			if target > pos {
				return "", 0, ErrForwardPointer
			}
			if !jumped {
				nextOffset = pos + 2
			}
			jumped = true
			jumps++
			if jumps > maxNameJumps {
				return "", 0, ErrPointerLoop
			}
			pos = target

		case length&0xC0 != 0: // 10xxxxxx / 01xxxxxx 为保留类型
			return "", 0, ErrReservedLabelType

		default: // 普通标签
			pos++
			if pos+length > len(msg) {
				return "", 0, ErrShortMessage
			}
			wireLen += length + 1
			if wireLen+1 > maxNameWireLen { // +1 为结尾根零字节
				return "", 0, ErrNameTooLong
			}
			if b.Len() > 0 {
				b.WriteByte('.')
			}
			for i := 0; i < length; i++ {
				c := msg[pos+i]
				if c >= 'A' && c <= 'Z' {
					c += 'a' - 'A'
				}
				b.WriteByte(c)
			}
			pos += length
		}
	}
}

// encodeName 把域名编码为线格式（含结尾零字节）。
func encodeName(name string) ([]byte, error) {
	canonical := CanonicalName(name)
	if canonical == "." {
		return []byte{0}, nil
	}
	trimmed := canonical[:len(canonical)-1]

	out := make([]byte, 0, len(trimmed)+2)
	wireLen := 0
	start := 0
	for i := 0; i <= len(trimmed); i++ {
		if i < len(trimmed) && trimmed[i] != '.' {
			continue
		}
		label := trimmed[start:i]
		if len(label) == 0 || len(label) > maxLabelLen {
			return nil, ErrLabelTooLong
		}
		for j := 0; j < len(label); j++ {
			c := label[j]
			switch {
			case c >= 'a' && c <= 'z', c >= '0' && c <= '9', c == '-', c == '_':
			default:
				return nil, ErrInvalidName
			}
		}
		wireLen += len(label) + 1
		if wireLen+1 > maxNameWireLen {
			return nil, ErrNameTooLong
		}
		out = append(out, byte(len(label)))
		out = append(out, label...)
		start = i + 1
	}
	out = append(out, 0)
	return out, nil
}

// BuildQuery 构建单问题、RD=1 的标准查询（仅支持 A/AAAA，IN 类）。
func BuildQuery(id uint16, name string, qtype uint16) ([]byte, error) {
	if qtype != TypeA && qtype != TypeAAAA {
		return nil, ErrInvalidQType
	}
	qname, err := encodeName(name)
	if err != nil {
		return nil, err
	}
	msg := make([]byte, headerLen+len(qname)+4)
	binary.BigEndian.PutUint16(msg[0:2], id)
	binary.BigEndian.PutUint16(msg[2:4], 0x0100) // RD
	binary.BigEndian.PutUint16(msg[4:6], 1)      // QDCOUNT
	copy(msg[headerLen:], qname)
	off := headerLen + len(qname)
	binary.BigEndian.PutUint16(msg[off:off+2], qtype)
	binary.BigEndian.PutUint16(msg[off+2:off+4], ClassIN)
	return msg, nil
}

// AnswerSpec 描述应答中的一个 A/AAAA 记录。
type AnswerSpec struct {
	Name string
	TTL  uint32
	IP   net.IP
}

// BuildResponse 构建对单问题查询的应答报文。
// 应答名称与问题一致时使用压缩指针（指向偏移 12 的问题名）。
func BuildResponse(id uint16, q Question, rcode byte, aa bool, answers []AnswerSpec) ([]byte, error) {
	qname, err := encodeName(q.Name)
	if err != nil {
		return nil, err
	}

	qclass := q.Class
	if qclass == 0 {
		qclass = ClassIN
	}

	var flags uint16 = 0x8000 | 0x0100 | 0x0080 // QR | RD | RA
	if aa {
		flags |= 0x0400
	}
	flags |= uint16(rcode) & 0x000F

	msg := make([]byte, 0, headerLen+len(qname)+4)
	buf := make([]byte, 2)
	putU16 := func(v uint16) {
		binary.BigEndian.PutUint16(buf, v)
		msg = append(msg, buf...)
	}

	// 头部
	hdr := make([]byte, headerLen)
	binary.BigEndian.PutUint16(hdr[0:2], id)
	binary.BigEndian.PutUint16(hdr[2:4], flags)
	binary.BigEndian.PutUint16(hdr[4:6], 1)
	binary.BigEndian.PutUint16(hdr[6:8], uint16(len(answers)))
	msg = append(msg, hdr...)

	// 问题段
	msg = append(msg, qname...)
	putU16(q.Type)
	putU16(qclass)

	qCanonical := CanonicalName(q.Name)
	for _, a := range answers {
		if CanonicalName(a.Name) == qCanonical {
			putU16(0xC000 | uint16(headerLen)) // 名称压缩指针 -> 12
		} else {
			aname, err := encodeName(a.Name)
			if err != nil {
				return nil, err
			}
			msg = append(msg, aname...)
		}
		qtype := uint16(TypeA)
		rdata := a.IP.To4()
		if rdata == nil {
			qtype = TypeAAAA
			rdata = a.IP.To16()
		}
		putU16(qtype)
		putU16(qclass)
		ttlBuf := make([]byte, 4)
		binary.BigEndian.PutUint32(ttlBuf, a.TTL)
		msg = append(msg, ttlBuf...)
		putU16(uint16(len(rdata)))
		msg = append(msg, rdata...)
	}
	return msg, nil
}
