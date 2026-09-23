package dnsmsg

import (
	"encoding/binary"
	"errors"
	"net"
	"testing"
)

func TestBuildQueryAndParseRoundTrip(t *testing.T) {
	q, err := BuildQuery(0xABCD, "Example.COM", TypeA)
	if err != nil {
		t.Fatalf("BuildQuery: %v", err)
	}
	if len(q) != headerLen+len("\x07example\x03com\x00")+4 {
		t.Fatalf("unexpected wire length %d", len(q))
	}

	m, err := ParseMessage(q)
	if err != nil {
		t.Fatalf("ParseMessage: %v", err)
	}
	if m.ID != 0xABCD || !m.RD || m.QR {
		t.Fatalf("unexpected header: %+v", m)
	}
	if len(m.Questions) != 1 {
		t.Fatalf("want 1 question, got %d", len(m.Questions))
	}
	qq := m.Questions[0]
	if qq.Name != "example.com." || qq.Type != TypeA || qq.Class != ClassIN {
		t.Fatalf("unexpected question: %+v", qq)
	}
}

// 正常压缩指针：应答名用 0xC00C 指向偏移 12 的问题名，必须解析成功。
func TestParseValidCompressionPointer(t *testing.T) {
	q := Question{Name: "example.com.", Type: TypeA, Class: ClassIN}
	resp, err := BuildResponse(1, q, 0, false, []AnswerSpec{
		{Name: "example.com.", TTL: 60, IP: net.ParseIP("192.0.2.10")},
	})
	if err != nil {
		t.Fatalf("BuildResponse: %v", err)
	}

	m, err := ParseMessage(resp)
	if err != nil {
		t.Fatalf("ParseMessage: %v", err)
	}
	if len(m.Answers) != 1 {
		t.Fatalf("want 1 answer, got %d", len(m.Answers))
	}
	a := m.Answers[0]
	if a.Name != "example.com." || a.TTL != 60 || a.IP.String() != "192.0.2.10" {
		t.Fatalf("unexpected answer: %+v ip=%v", a, a.IP)
	}
}

// 多跳压缩指针链（全部向后指）合法，应顺着链解析。
func TestParseMultiHopPointerChain(t *testing.T) {
	// 合法布局（解析器从偏移 12 顺序读取问题段，因此问题名必须在最前）：
	//   12: "example.com" 全名（13 字节，根字节在 24）
	//   25: QTYPE/QCLASS（结束于 28）
	//   29: answer1 名指针 -> 12（1 跳）+ RR（16 字节，结束于 44）
	//   45: answer2 名指针 -> 29（2 跳：45->29->12）+ RR
	var b []byte
	put := func(off int, data ...byte) {
		if off+len(data) > cap(b) {
			t.Fatalf("write [%d,%d) exceeds buffer cap %d", off, off+len(data), cap(b))
		}
		b = append(b, make([]byte, off+len(data)-len(b))...)
		copy(b[off:], data)
	}
	putPtr := func(off, target int) { put(off, 0xC0, byte(target)) }
	putU16 := func(off int, v uint16) { put(off, byte(v>>8), byte(v)) }
	putU32 := func(off int, v uint32) {
		put(off, byte(v>>24), byte(v>>16), byte(v>>8), byte(v))
	}
	putRR := func(off, nameTarget int, ipLast byte) {
		putPtr(off, nameTarget)
		putU16(off+2, TypeA)
		putU16(off+4, ClassIN)
		putU32(off+6, 10)
		putU16(off+10, 4)
		put(off+12, 192, 0, 2, ipLast)
	}

	b = make([]byte, 0, 64)

	hdr := make([]byte, headerLen)
	binary.BigEndian.PutUint16(hdr[2:4], 0x8180)
	binary.BigEndian.PutUint16(hdr[4:6], 1)
	binary.BigEndian.PutUint16(hdr[6:8], 2)
	b = append(b, hdr...)

	const qName = 12
	put(qName,
		7, 'e', 'x', 'a', 'm', 'p', 'l', 'e',
		3, 'c', 'o', 'm',
		0)
	putU16(25, TypeA)
	putU16(27, ClassIN)

	const ans1 = 29
	putRR(ans1, qName, 10) // answer1 名 -> 12
	const ans2 = 45
	putRR(ans2, ans1, 11) // answer2 名 -> 29，形成两跳

	m, err := ParseMessage(b)
	if err != nil {
		t.Fatalf("ParseMessage: %v", err)
	}
	if m.Questions[0].Name != "example.com." {
		t.Fatalf("question name = %q", m.Questions[0].Name)
	}
	if m.Answers[0].Name != "example.com." {
		t.Fatalf("answer1 name = %q", m.Answers[0].Name)
	}
	if m.Answers[1].Name != "example.com." {
		t.Fatalf("answer2 name = %q", m.Answers[1].Name)
	}
	if m.Answers[1].IP.String() != "192.0.2.11" {
		t.Fatalf("answer2 ip = %v", m.Answers[1].IP)
	}
}

// 从一个合法应答出发，在指定位置篡改压缩指针。
func mutateAnswerPointer(t *testing.T, resp []byte, pointer uint16) []byte {
	t.Helper()
	qname, err := encodeName("example.com.")
	if err != nil {
		t.Fatal(err)
	}
	ansOff := headerLen + len(qname) + 4
	out := append([]byte(nil), resp...)
	binary.BigEndian.PutUint16(out[ansOff:ansOff+2], pointer)
	return out
}

func validResponse(t *testing.T) []byte {
	t.Helper()
	q := Question{Name: "example.com.", Type: TypeA, Class: ClassIN}
	resp, err := BuildResponse(1, q, 0, false, []AnswerSpec{
		{Name: "example.com.", TTL: 60, IP: net.ParseIP("192.0.2.10")},
	})
	if err != nil {
		t.Fatal(err)
	}
	return resp
}

// 指针环（自指）：answer name 指针指向自己。
func TestPointerLoopSelf(t *testing.T) {
	resp := validResponse(t)
	qname, _ := encodeName("example.com.")
	ansOff := headerLen + len(qname) + 4

	bad := mutateAnswerPointer(t, resp, 0xC000|uint16(ansOff))
	if _, err := ParseMessage(bad); !errors.Is(err, ErrPointerLoop) {
		t.Fatalf("want ErrPointerLoop, got %v", err)
	}
}

// 指针环（互指）：两个指针都不可能在“只能向后指”的规则下成环，
// 因此互指必然包含前向指针 -> 必须被拒（ErrForwardPointer 或 ErrPointerLoop）。
func TestPointerLoopMutual(t *testing.T) {
	// 构造两个位置：offset 12 处放指针 -> 20（前向），20 处放名字根零字节。
	msg := make([]byte, 40)
	binary.BigEndian.PutUint16(msg[2:4], 0x8180)
	binary.BigEndian.PutUint16(msg[4:6], 1) // 1 question
	// offset 12: 指针 -> 20（前向）
	binary.BigEndian.PutUint16(msg[12:14], 0xC000|20)
	// offset 14..19 填充；offset 20: 指针 -> 12（成环）
	binary.BigEndian.PutUint16(msg[20:22], 0xC000|12)
	_, err := ParseMessage(msg)
	if !errors.Is(err, ErrForwardPointer) && !errors.Is(err, ErrPointerLoop) {
		t.Fatalf("want forward/loop error, got %v", err)
	}
}

// 越界偏移：指向报文之外、指向头部。
func TestPointerOutOfBounds(t *testing.T) {
	resp := validResponse(t)

	cases := map[string]uint16{
		"past end of packet": 0xC000 | uint16(len(resp)+100),
		"into header":        0xC000 | 6,
		"max offset":         0xFFFF,
	}
	for name, ptr := range cases {
		t.Run(name, func(t *testing.T) {
			bad := mutateAnswerPointer(t, resp, ptr)
			if _, err := ParseMessage(bad); !errors.Is(err, ErrPointerOutOfBounds) {
				t.Fatalf("want ErrPointerOutOfBounds, got %v", err)
			}
		})
	}
}

// 保留标签类型 10xxxxxx / 01xxxxxx 必须拒绝。
func TestReservedLabelType(t *testing.T) {
	msg := make([]byte, headerLen+8)
	binary.BigEndian.PutUint16(msg[4:6], 1)
	msg[12] = 0x40 // 01xxxxxx
	if _, err := ParseMessage(msg); !errors.Is(err, ErrReservedLabelType) {
		t.Fatalf("want ErrReservedLabelType, got %v", err)
	}

	msg[12] = 0x80 // 10xxxxxx（扩展标签，已废弃）
	if _, err := ParseMessage(msg); !errors.Is(err, ErrReservedLabelType) {
		t.Fatalf("want ErrReservedLabelType, got %v", err)
	}
}

// 截断响应：各类物理截断都必须返回 ErrShortMessage / ErrBadRDLength。
func TestTruncatedResponses(t *testing.T) {
	resp := validResponse(t)

	cases := map[string][]byte{
		"empty":               nil,
		"shorter than header": resp[:headerLen-1],
		"question name cut":   resp[:headerLen+2],
		"question tail cut":   func() []byte { q, _ := encodeName("example.com."); return resp[:headerLen+len(q)+2] }(),
		"answer fixed part cut": func() []byte {
			q, _ := encodeName("example.com.")
			return resp[:headerLen+len(q)+4+2+4] // 指针+type+class，缺 TTL/rdlength
		}(),
		"rdata exceeds packet": func() []byte {
			out := append([]byte(nil), resp...)
			// 头部 ANCOUNT=1 保留；把 RDLENGTH 改为远超报文长度。
			// answer 内布局：name(2) TYPE(2) CLASS(2) TTL(4) RDLENGTH(2)
			q, _ := encodeName("example.com.")
			rlOff := headerLen + len(q) + 4 + 10
			binary.BigEndian.PutUint16(out[rlOff:rlOff+2], 999)
			return out
		}(),
	}
	for name, pkt := range cases {
		t.Run(name, func(t *testing.T) {
			_, err := ParseMessage(pkt)
			if !errors.Is(err, ErrShortMessage) && !errors.Is(err, ErrBadRDLength) {
				t.Fatalf("want truncation error, got %v", err)
			}
		})
	}
}

// 报文声明的段数量超过安全上限。
func TestTooManyRecords(t *testing.T) {
	msg := make([]byte, headerLen)
	binary.BigEndian.PutUint16(msg[6:8], maxSectionCount+1)
	if _, err := ParseMessage(msg); !errors.Is(err, ErrTooManyRecords) {
		t.Fatalf("want ErrTooManyRecords, got %v", err)
	}
}

// 标签/名称长度上限。
func TestNameLengthLimits(t *testing.T) {
	// 64 字节单标签非法。
	bad := make([]byte, 0, 70)
	bad = append(bad, 64)
	for i := 0; i < 64; i++ {
		bad = append(bad, 'a')
	}
	bad = append(bad, 0)
	msg := append(make([]byte, headerLen), bad...)
	binary.BigEndian.PutUint16(msg[4:6], 1)
	if _, err := ParseMessage(msg); !errors.Is(err, ErrLabelTooLong) {
		// 64 以 01 开头属于保留标签类型（01000000），两种拒绝都算安全。
		if !errors.Is(err, ErrReservedLabelType) {
			t.Fatalf("want label/reserved error, got %v", err)
		}
	}

	// 线格式名称超过 255 字节：多个 63 字节标签。
	long := make([]byte, 0, 300)
	for total := 0; total <= maxNameWireLen; total += 64 {
		long = append(long, 63)
		for i := 0; i < 63; i++ {
			long = append(long, 'a')
		}
	}
	long = append(long, 0)
	msg2 := append(make([]byte, headerLen), long...)
	binary.BigEndian.PutUint16(msg2[4:6], 1)
	if _, err := ParseMessage(msg2); !errors.Is(err, ErrNameTooLong) {
		t.Fatalf("want ErrNameTooLong, got %v", err)
	}
}

func TestBuildQueryRejectsUnsupportedType(t *testing.T) {
	if _, err := BuildQuery(1, "example.com.", 15); !errors.Is(err, ErrInvalidQType) {
		t.Fatalf("want ErrInvalidQType, got %v", err)
	}
}

func TestEncodeNameRejectsBadNames(t *testing.T) {
	for _, name := range []string{"bad name.", "ex..ample.com."} {
		if _, err := encodeName(name); err == nil {
			t.Fatalf("expected error encoding %q", name)
		}
	}
}
