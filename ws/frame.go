package ws

// 帧操作码 (RFC 6455 §5.2)。
const (
	OpContinuation = 0x0
	OpText         = 0x1
	OpBinary       = 0x2
	OpClose        = 0x8
	OpPing         = 0x9
	OpPong         = 0xA
)

// 关闭状态码 (RFC 6455 §7.4)。
const (
	CloseNormalClosure           = 1000
	CloseGoingAway               = 1001
	CloseProtocolError           = 1002
	CloseUnsupportedData         = 1003
	CloseNoStatusReceived        = 1005
	CloseAbnormalClosure         = 1006
	CloseInvalidFramePayloadData = 1007
	ClosePolicyViolation         = 1008
	CloseMessageTooLarge         = 1009
	CloseMandatoryExtension      = 1010
	CloseInternalServerErr       = 1011
)

// 默认大小限制。
const (
	DefaultMaxFrameSize   = 1 << 20 // 1 MiB（不含帧头）
	DefaultMaxMessageSize = 4 << 20 // 4 MiB
	maxControlFrameSize   = 125     // 协议硬性规定
)

// Event 是解码器吐出的一个结果。同一类型以外的字段无效。
type Event struct {
	// Kind 为 "message" / "ping" / "pong" / "close" 之一。
	Kind string
	// OpCode 仅对 message 有意义：OpText 或 OpBinary。
	OpCode int
	// Data：
	//   message —— 重组完成的整条消息（独立缓冲，调用方可长期持有）；
	//   ping/pong —— 应用数据（独立缓冲）；
	//   close —— 关闭载荷，前 2 字节（大端）为状态码，其余为 UTF-8 原因。
	Data []byte
}

const (
	eventMessage = "message"
	eventPing    = "ping"
	eventPong    = "pong"
	eventClose   = "close"
)
