package ws

import (
	"crypto/rand"
	"crypto/sha1"
	"encoding/base64"
	"encoding/binary"
	"io"
)

// FrameWriter 把载荷编码成 RFC 6455 帧并写入底层 io.Writer。
// Mask=true 用于客户端（必须掩码），服务端置 false。
type FrameWriter struct {
	W    io.Writer
	Mask bool

	// MaxMessageSize 仅用于 WriteMessage 对整条消息做一次上限检查。
	MaxMessageSize int64

	maskKey [4]byte
}

// WriteFrame 写出单个帧。控制帧（opcode >= 0x8）必须 fin=true 且
// 载荷不超过 125 字节，否则返回 ErrInvalidFrame。
func (fw *FrameWriter) WriteFrame(fin bool, opcode int, payload []byte) error {
	if opcode >= 0x8 {
		if !fin {
			return ErrInvalidFrame
		}
		if len(payload) > maxControlFrameSize {
			return ErrInvalidFrame
		}
	}

	var header [14]byte // 2 + 8 + 4
	n := 0
	if fin {
		header[0] = 0x80 | byte(opcode)
	} else {
		header[0] = byte(opcode)
	}

	maskBit := byte(0)
	if fw.Mask {
		maskBit = 0x80
		if _, err := io.ReadFull(rand.Reader, fw.maskKey[:]); err != nil {
			return err
		}
	}

	l := len(payload)
	switch {
	case l <= 125:
		header[1] = maskBit | byte(l)
		n = 2
	case l <= 0xFFFF:
		header[1] = maskBit | 126
		binary.BigEndian.PutUint16(header[2:4], uint16(l))
		n = 4
	default:
		header[1] = maskBit | 127
		binary.BigEndian.PutUint64(header[2:10], uint64(l))
		n = 10
	}
	if fw.Mask {
		copy(header[n:n+4], fw.maskKey[:])
		n += 4
	}

	// 关键：掩码帧的头部（含 mask key）和去掩码后的载荷必须尽量在一次
	// Write 中发出，否则它们会成为两个 TCP 段，对端可能先读到只有头的帧。
	// 小帧直接组到一个缓冲里；大帧退化为“头一次 + 载荷流式异或”。
	const inlined = 32 * 1024
	if fw.Mask && l <= inlined {
		frame := make([]byte, 0, n+l)
		frame = append(frame, header[:n]...)
		for i := 0; i < l; i++ {
			frame = append(frame, payload[i]^fw.maskKey[i%4])
		}
		_, err := fw.W.Write(frame)
		return err
	}

	if _, err := fw.W.Write(header[:n]); err != nil {
		return err
	}
	if !fw.Mask {
		_, err := fw.W.Write(payload)
		return err
	}

	// 掩码写入：不修改调用方的 payload，用固定大小暂存块分块异或。
	var scratch [4096]byte
	for off := 0; off < len(payload); {
		chunk := scratch[:]
		if remain := len(payload) - off; remain < len(chunk) {
			chunk = chunk[:remain]
		}
		for i := range chunk {
			chunk[i] = payload[off+i] ^ fw.maskKey[(off+i)%4]
		}
		if _, err := fw.W.Write(chunk); err != nil {
			return err
		}
		off += len(chunk)
	}
	return nil
}

// WriteMessage 以单个未分片帧写出一条完整消息。
func (fw *FrameWriter) WriteMessage(opcode int, payload []byte) error {
	if fw.MaxMessageSize > 0 && int64(len(payload)) > fw.MaxMessageSize {
		return ErrTooLarge
	}
	return fw.WriteFrame(true, opcode, payload)
}

// WritePing / WritePong 写出心跳帧。
func (fw *FrameWriter) WritePing(appData []byte) error {
	return fw.WriteFrame(true, OpPing, appData)
}

func (fw *FrameWriter) WritePong(appData []byte) error {
	return fw.WriteFrame(true, OpPong, appData)
}

// WriteClose 写出关闭帧。code<=0 时发送空载荷关闭帧。
func (fw *FrameWriter) WriteClose(code int, reason string) error {
	if code <= 0 {
		return fw.WriteFrame(true, OpClose, nil)
	}
	if len(reason) > maxControlFrameSize-2 {
		return ErrTooLarge
	}
	payload := make([]byte, 2+len(reason))
	payload[0] = byte(code >> 8)
	payload[1] = byte(code)
	copy(payload[2:], reason)
	return fw.WriteFrame(true, OpClose, payload)
}

// CloseCode 从 close 事件的 Data 中解析状态码；无状态码时返回 0。
func CloseCode(data []byte) int {
	if len(data) < 2 {
		return 0
	}
	return int(data[0])<<8 | int(data[1])
}

// CloseReason 从 close 事件的 Data 中解析 UTF-8 关闭原因。
func CloseReason(data []byte) string {
	if len(data) <= 2 {
		return ""
	}
	return string(data[2:])
}

// AcceptKey 按 RFC 6455 §4.2.2 计算 Sec-WebSocket-Accept，
// 客户端可用它校验服务端握手响应。
func AcceptKey(key string) string {
	h := sha1.Sum([]byte(key + acceptGUID))
	return base64.StdEncoding.EncodeToString(h[:])
}
