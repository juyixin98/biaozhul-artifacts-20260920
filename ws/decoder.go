package ws

import "unicode/utf8"

// Decoder 是本地 WebSocket 帧状态机。
//
// 它不知道任何 I/O：调用方把从网络读到的任意大小分块通过 Feed 喂入，
// 解码器负责去掩码、控制帧穿插、分片消息重组、跨分片 UTF-8 校验和
// 大小限制。任何一次协议违例都会返回不可恢复的错误，此后 Decoder
// 不可再用。
type Decoder struct {
	RequireMask    bool // 服务端必须为 true：客户端帧必须带掩码
	MaxFrameSize   int64
	MaxMessageSize int64

	buf     []byte // 尚未解析完的字节（可能只有半帧头/半载荷）
	parsed  int    // buf 中已解析前缀的长度
	msg     []byte // 正在重组的消息
	msgOp   int    // 消息起始帧操作码：OpText / OpBinary；0 表示空闲
	msgSize int64  // 已重组的消息字节数
	utf8    utf8Validator
	closed  bool // 已收到并吐出 Close 帧
}

// NewDecoder 创建服务端侧解码器（RequireMask=true，默认大小限制）。
func NewDecoder() *Decoder {
	return &Decoder{
		RequireMask:    true,
		MaxFrameSize:   DefaultMaxFrameSize,
		MaxMessageSize: DefaultMaxMessageSize,
	}
}

// Feed 喂入一段新读到的网络数据，返回本次数据中解析出的全部事件。
// data 会被立即消费，返回事件中的 Data 均为独立拷贝，不引用入参。
// 一旦返回非 nil 错误，连接必须按错误中的关闭码结束。
func (d *Decoder) Feed(data []byte) ([]Event, error) {
	if d.closed {
		return nil, ErrClosed
	}
	// 把上次未解析完的尾部压实到缓冲开头，再追加新数据。
	if d.parsed > 0 {
		d.buf = append(d.buf[:0], d.buf[d.parsed:]...)
		d.parsed = 0
	}
	d.buf = append(d.buf, data...)

	var events []Event
	for d.parsed < len(d.buf) {
		if d.closed {
			// Close 必须是最后一帧；其后还有字节属于协议违例。
			return events, protoError(CloseProtocolError, "data received after close frame")
		}
		ev, n, err := d.parseFrameAt(d.buf[d.parsed:])
		if err != nil {
			return events, err
		}
		if n == 0 {
			break // 帧不完整，等待更多数据
		}
		d.parsed += n
		if ev != nil {
			events = append(events, *ev)
		}
	}
	return events, nil
}

// parseFrameAt 尝试从 b 的开头解析一个完整帧。
// consumed 为 false 表示数据不足（此时永不会修改消息状态）。
func (d *Decoder) parseFrameAt(b []byte) (ev *Event, consumed int, err error) {
	if len(b) < 2 {
		return nil, 0, nil
	}
	b0, b1 := b[0], b[1]

	fin := b0&0x80 != 0
	rsv := b0 & 0x70
	op := int(b0 & 0x0F)
	masked := b1&0x80 != 0
	payloadLen := int64(b1 & 0x7F)
	headerLen := 2

	if rsv != 0 {
		return nil, 0, protoError(CloseProtocolError, "RSV bits must be zero (no extensions negotiated)")
	}
	if d.RequireMask && !masked {
		return nil, 0, protoError(CloseProtocolError, "client frame must be masked")
	}
	if !d.RequireMask && masked {
		return nil, 0, protoError(CloseProtocolError, "server frame must not be masked")
	}

	switch payloadLen {
	case 126:
		if len(b) < 4 {
			return nil, 0, nil
		}
		payloadLen = int64(b[2])<<8 | int64(b[3])
		headerLen = 4
	case 127:
		if len(b) < 10 {
			return nil, 0, nil
		}
		payloadLen = 0
		for i := 2; i < 10; i++ {
			payloadLen = payloadLen<<8 | int64(b[i])
		}
		headerLen = 10
		// 长度最高位为 1 的帧无法在不超过 int64 的情况下被处理。
		if b[2]&0x80 != 0 {
			return nil, 0, protoError(CloseProtocolError, "frame payload length exceeds 63 bits")
		}
	}

	var maskKey [4]byte
	if masked {
		if len(b) < headerLen+4 {
			return nil, 0, nil
		}
		copy(maskKey[:], b[headerLen:headerLen+4])
		headerLen += 4
	}

	if int64(len(b)) < int64(headerLen)+payloadLen {
		return nil, 0, nil // 帧载荷不全
	}

	frameEnd := int64(headerLen) + payloadLen
	payload := b[headerLen:frameEnd]
	if masked {
		// 原地去掩码。注意 b 是 d.buf 的切片，这里只改的是本次帧载荷区，
		// 已解析前缀不再使用，安全。
		for i := range payload {
			payload[i] ^= maskKey[i%4]
		}
	}

	isControl := op >= 0x8
	if isControl {
		if !fin {
			return nil, 0, protoError(CloseProtocolError, "control frames must not be fragmented")
		}
		if payloadLen > maxControlFrameSize {
			return nil, 0, protoError(CloseProtocolError, "control frame payload exceeds 125 bytes")
		}
		switch op {
		case OpPing:
			return &Event{Kind: eventPing, Data: cloneBytes(payload)}, int(frameEnd), nil
		case OpPong:
			return &Event{Kind: eventPong, Data: cloneBytes(payload)}, int(frameEnd), nil
		case OpClose:
			return d.parseClose(payload, frameEnd)
		default:
			return nil, 0, protoError(CloseProtocolError, "reserved control opcode")
		}
	}

	// 数据帧：op 只能是 continuation/text/binary。
	switch op {
	case OpContinuation, OpText, OpBinary:
	default:
		return nil, 0, protoError(CloseProtocolError, "reserved data opcode")
	}

	// 帧大小限制（数据帧）。
	if d.MaxFrameSize > 0 && payloadLen > d.MaxFrameSize {
		return nil, 0, protoError(CloseMessageTooLarge, "frame payload exceeds limit")
	}

	starting := op == OpText || op == OpBinary
	if starting {
		if d.msgOp != 0 {
			return nil, 0, protoError(CloseProtocolError, "new data frame started before previous fragmented message finished")
		}
		d.msgOp = op
	} else { // continuation
		if d.msgOp == 0 {
			return nil, 0, protoError(CloseProtocolError, "continuation frame without a starting frame")
		}
	}

	// 消息大小限制。
	if d.MaxMessageSize > 0 && d.msgSize+payloadLen > d.MaxMessageSize {
		return nil, 0, protoError(CloseMessageTooLarge, "assembled message exceeds limit")
	}

	// 文本消息需要增量 UTF-8 校验；二进制不校验。
	if d.msgOp == OpText {
		if err := d.utf8.validate(payload, fin); err != nil {
			return nil, 0, err
		}
	}

	d.msg = append(d.msg, payload...)
	d.msgSize += payloadLen

	if fin {
		opCode := d.msgOp
		msg := d.msg
		d.msg = nil
		d.msgOp = 0
		d.msgSize = 0
		d.utf8.reset()
		return &Event{Kind: eventMessage, OpCode: opCode, Data: msg}, int(frameEnd), nil
	}
	return nil, int(frameEnd), nil
}

// parseClose 校验关闭帧载荷（RFC 6455 §5.5.1 / §7.4）。
func (d *Decoder) parseClose(payload []byte, frameEnd int64) (*Event, int, error) {
	switch len(payload) {
	case 0:
		// 无状态码是允许的。
	case 1:
		return nil, 0, protoError(CloseProtocolError, "close frame with a body must carry a 2-byte status code")
	default:
		code := int(payload[0])<<8 | int(payload[1])
		if !validCloseCode(code) {
			return nil, 0, protoError(CloseProtocolError, "invalid close status code")
		}
		if !utf8Bytes(payload[2:]) {
			return nil, 0, protoError(CloseInvalidFramePayloadData, "close reason must be valid UTF-8")
		}
	}
	d.closed = true
	return &Event{Kind: eventClose, Data: cloneBytes(payload)}, int(frameEnd), nil
}

// validCloseCode 判断状态码能否出现在 Close 帧中。
func validCloseCode(code int) bool {
	switch {
	case code >= 3000 && code <= 4999:
		return true // 私有/注册扩展码
	case code >= 1000 && code <= 1003,
		code == 1007, code == 1008,
		code >= 1009 && code <= 1011,
		code >= 1012 && code <= 1014,
		code == 1015:
		return true
	default:
		return false
	}
}

func cloneBytes(b []byte) []byte {
	if len(b) == 0 {
		return nil
	}
	out := make([]byte, len(b))
	copy(out, b)
	return out
}

func utf8Bytes(b []byte) bool {
	return utf8.Valid(b)
}
