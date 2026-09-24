// Package rt 实现一个 UDP 可靠传输子集的模拟：
// 序号 + 累积 ACK + 滑动窗口 + 超时/快速重传 + 整体哈希校验，
// 并支持连接代际（generation）区分新旧连接。
//
// 本包不直接依赖 net 包：所有报文收发都经过 Link 接口，
// 因而既可以跑在内存链路上（确定性、可注入故障），
// 也可以跑在真实 UDP 回环链路上。
package rt

import (
	"encoding/binary"
	"errors"
	"fmt"
	"hash"
	"io"
)

// 报文类型。
const (
	MsgSYN    byte = 1 // 连接建立（发起方 -> 接收方）
	MsgSYNACK byte = 2 // 连接建立确认
	MsgDATA   byte = 3 // 数据
	MsgACK    byte = 4 // 累积确认
	MsgFIN    byte = 5 // 结束：携带总长与整体哈希
	MsgFINACK byte = 6 // 结束确认
	MsgERROR  byte = 7 // 致命错误（如整体哈希不匹配）
)

// Wire 常量。
const (
	// wireHeaderLen: type(1) + seq(4) + ack(4) + gen(8) + payloadLen(4) = 21
	wireHeaderLen = 1 + 4 + 4 + 8 + 4
	// UDPMaxPayload 是 UDP 载荷的保守上限（65535 - 20 IP - 8 UDP）。
	UDPMaxPayload = 65507
)

// Packet 是协议报文的内存表示。
type Packet struct {
	Type    byte
	Seq     uint32 // DATA: 数据序号；FIN: 最后一个数据序号 +1（回绕）
	Ack     uint32 // 累积确认：已连续收到的最后序号 +1
	Gen     uint64 // 连接代际
	Payload []byte // DATA: 数据块；FIN: 元数据；ERROR: 错误文本
}

// FIN 报文 Payload 布局：totalLen(8) + hashLen(2) + hash。
const finMetaPrefix = 8 + 2

// Marshal 把报文序列化为二进制。返回的字节切片不引用内部 Payload。
func (p *Packet) Marshal() ([]byte, error) {
	if len(p.Payload) > 0xffffffff {
		return nil, errors.New("rt: payload too large")
	}
	buf := make([]byte, wireHeaderLen+len(p.Payload))
	buf[0] = p.Type
	binary.BigEndian.PutUint32(buf[1:5], p.Seq)
	binary.BigEndian.PutUint32(buf[5:9], p.Ack)
	binary.BigEndian.PutUint64(buf[9:17], p.Gen)
	binary.BigEndian.PutUint32(buf[17:21], uint32(len(p.Payload)))
	copy(buf[21:], p.Payload)
	return buf, nil
}

// UnmarshalPacket 解析二进制报文。
func UnmarshalPacket(b []byte) (*Packet, error) {
	if len(b) < wireHeaderLen {
		return nil, fmt.Errorf("rt: short packet: %d bytes", len(b))
	}
	n := binary.BigEndian.Uint32(b[17:21])
	if int(n) != len(b)-wireHeaderLen {
		return nil, fmt.Errorf("rt: payload length mismatch: header says %d, got %d", n, len(b)-wireHeaderLen)
	}
	p := &Packet{
		Type: b[0],
		Seq:  binary.BigEndian.Uint32(b[1:5]),
		Ack:  binary.BigEndian.Uint32(b[5:9]),
		Gen:  binary.BigEndian.Uint64(b[9:17]),
	}
	if n > 0 {
		p.Payload = make([]byte, n)
		copy(p.Payload, b[21:])
	}
	return p, nil
}

// marshalFIN 构造 FIN 的元数据载荷。
func marshalFIN(totalLen uint64, sum []byte) []byte {
	buf := make([]byte, finMetaPrefix+len(sum))
	binary.BigEndian.PutUint64(buf[0:8], totalLen)
	binary.BigEndian.PutUint16(buf[8:10], uint16(len(sum)))
	copy(buf[10:], sum)
	return buf
}

// unmarshalFIN 解析 FIN 的元数据载荷。
func unmarshalFIN(p []byte) (uint64, []byte, error) {
	if len(p) < finMetaPrefix {
		return 0, nil, errors.New("rt: short FIN metadata")
	}
	hl := binary.BigEndian.Uint16(p[8:10])
	if int(hl) != len(p)-finMetaPrefix {
		return 0, nil, errors.New("rt: FIN hash length mismatch")
	}
	sum := make([]byte, hl)
	copy(sum, p[finMetaPrefix:])
	return binary.BigEndian.Uint64(p[0:8]), sum, nil
}

// marshalSYN 构造 SYN 载荷：起始序号(4) + MSS(4)。
func marshalSYN(startSeq uint32, mss int) []byte {
	b := make([]byte, 8)
	binary.BigEndian.PutUint32(b[0:4], startSeq)
	binary.BigEndian.PutUint32(b[4:8], uint32(mss))
	return b
}

func unmarshalSYN(p []byte) (uint32, int, error) {
	if len(p) < 8 {
		return 0, 0, errors.New("rt: short SYN payload")
	}
	return binary.BigEndian.Uint32(p[0:4]),
		int(binary.BigEndian.Uint32(p[4:8])),
		nil
}

/*
序号回绕比较（32 位无符号模 2^32 空间）。

约定：seq 空间为环。a 在环上“落后于”b（a < b 模 2^32）当且仅当
(b-a) mod 2^32 落在开半区间 (0, 2^31) 内。即两序号的环距离
（从 a 正向走到 b 的步数）小于半圈。

  - seqLess(a,b): a 在 b 后面（a 更旧）
  - seqLE:        a == b 或 a 在 b 后面
  - seqInWindow(base, x, n): x ∈ [base, base+n)（环上），
    用于判断收到的 DATA/ACK 是否落在当前滑动窗口内。
    n 为窗口长度（报文个数），要求 1 <= n < 2^31。
*/

const seqHalf = uint32(1) << 31

func seqLess(a, b uint32) bool {
	d := b - a // 无符号减法即模 2^32 减法
	return d != 0 && d < seqHalf
}

func seqLE(a, b uint32) bool {
	return a == b || seqLess(a, b)
}

func seqInWindow(base, x uint32, n int) bool {
	d := x - base
	return uint64(d) < uint64(n)
}

// windowUsed 返回窗口内已占用的报文个数：(una..next) 的环距离。
func windowUsed(una, next uint32) int {
	return int(next - una)
}

// hashWriter 同时写入底层 writer 与 hash。
type hashWriter struct {
	w io.Writer
	h hash.Hash
}

func (hw hashWriter) Write(p []byte) (int, error) {
	hw.h.Write(p)
	return hw.w.Write(p)
}
