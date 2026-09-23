package ws

import "errors"

// 发送端在消息/帧超过对端声明的限制时使用；接收端遇到超限会产生
// CloseCode 为 1009 的 FrameError。
var (
	// ErrClosed 在关闭握手完成后继续向解码器喂入数据时返回。
	ErrClosed = errors.New("ws: connection closed")
	// ErrTooLarge 由写入端在消息或帧超过配置上限时返回。
	ErrTooLarge = errors.New("ws: message or frame too large")
	// ErrInvalidFrame 由写入端在参数本身不合法时返回（如控制帧 >125 字节）。
	ErrInvalidFrame = errors.New("ws: invalid frame")
)

// FrameError 表示违反 RFC 6455 的协议错误，携带建议的关闭状态码。
type FrameError struct {
	Code int
	Msg  string
}

func (e *FrameError) Error() string { return "ws: protocol error: " + e.Msg }

func protoError(code int, msg string) *FrameError {
	return &FrameError{Code: code, Msg: msg}
}
