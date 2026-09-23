package ws

import (
	"bufio"
	"encoding/base64"
	"errors"
	"io"
	"net"
	"net/http"
	"strings"
)

const acceptGUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11" // RFC 6455 §4.2.2

// Upgrader 把一个 HTTP 请求升级为 WebSocket 连接。
type Upgrader struct {
	// CheckOrigin 为 nil 时不校验 Origin（本地回环演示用途）。
	CheckOrigin func(r *http.Request) bool
	// 大小限制；0 表示使用默认值。
	MaxFrameSize   int64
	MaxMessageSize int64
}

// Conn 是升级后的服务端 WebSocket 连接。
type Conn struct {
	raw    net.Conn
	br     *bufio.Reader
	dec    *Decoder
	fw     *FrameWriter
	closed bool
}

// Upgrade 执行 RFC 6455 §4.2 握手校验并接管连接。
// 握手失败时已经向客户端写好 400 响应，返回的错误仅用于日志。
func (u Upgrader) Upgrade(w http.ResponseWriter, r *http.Request) (*Conn, error) {
	if !headerToken(r.Header, "Connection", "upgrade") ||
		!strings.EqualFold(r.Header.Get("Upgrade"), "websocket") ||
		r.Method != http.MethodGet {
		http.Error(w, "not a websocket handshake", http.StatusBadRequest)
		return nil, errors.New("ws: missing Upgrade/Connection headers or wrong method")
	}
	if r.ProtoMajor != 1 || r.ProtoMinor != 1 {
		http.Error(w, "only HTTP/1.1 websocket is supported", http.StatusBadRequest)
		return nil, errors.New("ws: unsupported HTTP version")
	}
	if r.Header.Get("Sec-WebSocket-Version") != "13" {
		w.Header().Set("Sec-WebSocket-Version", "13")
		http.Error(w, "unsupported websocket version", http.StatusUpgradeRequired)
		return nil, errors.New("ws: unsupported Sec-WebSocket-Version")
	}
	key := r.Header.Get("Sec-WebSocket-Key")
	if decode, err := base64.StdEncoding.DecodeString(key); err != nil || len(decode) != 16 {
		http.Error(w, "invalid Sec-WebSocket-Key", http.StatusBadRequest)
		return nil, errors.New("ws: invalid Sec-WebSocket-Key")
	}
	if u.CheckOrigin != nil && !u.CheckOrigin(r) {
		http.Error(w, "origin not allowed", http.StatusForbidden)
		return nil, errors.New("ws: origin rejected")
	}

	hj, ok := w.(http.Hijacker)
	if !ok {
		http.Error(w, "server does not support hijacking", http.StatusInternalServerError)
		return nil, errors.New("ws: ResponseWriter is not hijackable")
	}
	conn, brw, err := hj.Hijack()
	if err != nil {
		return nil, err
	}

	// 握手可能携带了请求体以外的缓冲字节（实现上通常没有，但按接口处理）。
	netConn := conn
	if brw.Reader.Buffered() > 0 {
		netConn = &prefixedConn{Conn: conn, prefix: brw.Reader}
	}

	accept := computeAccept(key)
	if _, err := io.WriteString(conn,
		"HTTP/1.1 101 Switching Protocols\r\n"+
			"Upgrade: websocket\r\n"+
			"Connection: Upgrade\r\n"+
			"Sec-WebSocket-Accept: "+accept+"\r\n\r\n"); err != nil {
		conn.Close()
		return nil, err
	}

	maxFrame := u.MaxFrameSize
	if maxFrame <= 0 {
		maxFrame = DefaultMaxFrameSize
	}
	maxMsg := u.MaxMessageSize
	if maxMsg <= 0 {
		maxMsg = DefaultMaxMessageSize
	}

	c := &Conn{
		raw: netConn,
		br:  bufio.NewReader(netConn),
		dec: &Decoder{RequireMask: true, MaxFrameSize: maxFrame, MaxMessageSize: maxMsg},
		fw:  &FrameWriter{W: conn, MaxMessageSize: maxMsg},
	}
	return c, nil
}

// prefixedConn 先吐出 Hijack 时 bufio.Reader 中已缓冲的字节，再走原连接。
type prefixedConn struct {
	net.Conn
	prefix *bufio.Reader
}

func (p *prefixedConn) Read(b []byte) (int, error) {
	if p.prefix != nil && p.prefix.Buffered() > 0 {
		return p.prefix.Read(b)
	}
	p.prefix = nil
	return p.Conn.Read(b)
}

func computeAccept(key string) string {
	return AcceptKey(key)
}

// headerToken 检查 headerName 里是否包含目标 token（大小写不敏感，
// Connection 头允许 "keep-alive, Upgrade" 这种逗号列表）。
func headerToken(h http.Header, headerName, want string) bool {
	for _, part := range strings.Split(h.Get(headerName), ",") {
		if strings.EqualFold(strings.TrimSpace(part), want) {
			return true
		}
	}
	return false
}

// ReadEvents 阻塞读取至少一个字节并解码事件。返回 io.EOF 表示 TCP 已关闭。
func (c *Conn) ReadEvents() ([]Event, error) {
	buf := make([]byte, 32*1024)
	n, err := c.br.Read(buf)
	if n > 0 {
		events, derr := c.dec.Feed(buf[:n])
		if derr != nil {
			return events, derr
		}
		if err != nil {
			return events, err // 可能是 io.EOF
		}
		return events, nil
	}
	if err != nil {
		return nil, err
	}
	// Read 合约允许返回 (0, nil)，对面向连接的套接字而言意味着连接异常。
	return nil, io.ErrUnexpectedEOF
}

// WriteMessage 发送一条文本/二进制消息。
func (c *Conn) WriteMessage(opcode int, payload []byte) error {
	return c.fw.WriteMessage(opcode, payload)
}

// WritePong 回应心跳。
func (c *Conn) WritePong(appData []byte) error {
	return c.fw.WritePong(appData)
}

// SendClose 发送关闭帧并标记关闭；不回收 TCP 连接（由 Close 完成）。
func (c *Conn) SendClose(code int, reason string) error {
	if c.closed {
		return nil
	}
	c.closed = true
	return c.fw.WriteClose(code, reason)
}

// Close 关闭底层 TCP 连接。
func (c *Conn) Close() error { return c.raw.Close() }

// RawConn 暴露底层连接用于设置读写超时等。
func (c *Conn) RawConn() net.Conn { return c.raw }
