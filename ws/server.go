package ws

import (
	"bufio"
	"crypto/sha1"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"time"
)

// websocketGUID is the magic GUID from RFC 6455 §4.2.2.
const websocketGUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

// closeWaitTimeout bounds how long the server keeps reading after sending its
// close frame, to give the peer time to finish the closing handshake.
const closeWaitTimeout = 5 * time.Second

// Logger is the minimal logging surface used by Serve. The standard *log.Logger
// satisfies it. A nil logger discards everything.
type Logger interface {
	Printf(format string, args ...any)
}

type discardLogger struct{}

func (discardLogger) Printf(string, ...any) {}

// Options configures the default echo server.
type Options struct {
	// MaxFrame bounds a single frame payload. Default 1 MiB.
	MaxFrame int
	// MaxMessage bounds a reassembled message. Default 4 MiB.
	MaxMessage int
	Logger     Logger
}

// CloseError is returned by Conn.ReadMessage when the closing handshake ended.
type CloseError struct {
	Code   int
	Reason string
	// SentByPeer is true when the peer initiated the close; false when *we*
	// initiated it (e.g. reacting to a protocol error).
	SentByPeer bool
}

func (e *CloseError) Error() string {
	if e.Reason != "" {
		return fmt.Sprintf("websocket: closed by peer code=%d reason=%q", e.Code, e.Reason)
	}
	return fmt.Sprintf("websocket: closed code=%d", e.Code)
}

// Conn is one upgraded WebSocket connection.
type Conn struct {
	br            *bufio.Reader
	wc            net.Conn
	parser        *FrameParser
	reassembler   *Reassembler
	logger        Logger
	writeDeadline time.Duration
}

// Upgrade performs the RFC 6455 §4.2 opening handshake over HTTP and returns
// the hijacked connection. All failure modes are written as normal HTTP error
// responses, so the caller need not write anything on error.
func Upgrade(w http.ResponseWriter, r *http.Request, maxFrame int) (*Conn, error) {
	if maxFrame <= 0 {
		maxFrame = 1 << 20
	}
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return nil, protoErr("WebSocket handshake requires GET")
	}
	if !headerContains(r.Header, "Connection", "upgrade") {
		http.Error(w, "expected Connection: Upgrade", http.StatusBadRequest)
		return nil, protoErr("missing Connection: Upgrade")
	}
	if !strings.EqualFold(r.Header.Get("Upgrade"), "websocket") {
		http.Error(w, "expected Upgrade: websocket", http.StatusBadRequest)
		return nil, protoErr("missing Upgrade: websocket")
	}
	if r.Header.Get("Sec-WebSocket-Version") != "13" {
		w.Header().Set("Sec-WebSocket-Version", "13")
		http.Error(w, "unsupported Sec-WebSocket-Version", http.StatusBadRequest)
		return nil, protoErr("unsupported Sec-WebSocket-Version")
	}
	key := r.Header.Get("Sec-WebSocket-Key")
	if decode, err := base64.StdEncoding.DecodeString(key); err != nil || len(decode) != 16 {
		http.Error(w, "invalid Sec-WebSocket-Key", http.StatusBadRequest)
		return nil, protoErr("invalid Sec-WebSocket-Key")
	}

	hj, ok := w.(http.Hijacker)
	if !ok {
		http.Error(w, "server does not support hijacking", http.StatusInternalServerError)
		return nil, protoErr("http.ResponseWriter is not a http.Hijacker")
	}
	conn, brw, err := hj.Hijack()
	if err != nil {
		return nil, err
	}

	accept := computeAccept(key)
	var resp strings.Builder
	resp.WriteString("HTTP/1.1 101 Switching Protocols\r\n")
	resp.WriteString("Upgrade: websocket\r\n")
	resp.WriteString("Connection: Upgrade\r\n")
	resp.WriteString("Sec-WebSocket-Accept: " + accept + "\r\n")
	resp.WriteString("\r\n")
	if _, err := io.WriteString(conn, resp.String()); err != nil {
		conn.Close()
		return nil, err
	}

	// Use the hijack buffer's reader so bytes already buffered by net/http
	// (frames sent immediately after the handshake) are not lost.
	br := brw.Reader
	if br == nil {
		br = bufio.NewReader(conn)
	}
	return &Conn{
		br:            br,
		wc:            conn,
		parser:        NewFrameParser(maxFrame),
		reassembler:   NewReassembler(ReassemblerConfig{MaxMessage: 4 << 20, RequireMask: true}),
		logger:        discardLogger{},
		writeDeadline: 10 * time.Second,
	}, nil
}

// AcceptKey computes the Sec-WebSocket-Accept value for a challenge key
// (RFC 6455 §4.2.2). Exported so clients can verify the server response.
func AcceptKey(challengeKey string) string {
	return computeAccept(challengeKey)
}

func computeAccept(key string) string {
	h := sha1.Sum([]byte(key + websocketGUID))
	return base64.StdEncoding.EncodeToString(h[:])
}

// headerContains performs the case-insensitive token-list contains check used
// for the Connection header (RFC 6455 §4.2.1).
func headerContains(h http.Header, name, token string) bool {
	for _, v := range h[http.CanonicalHeaderKey(name)] {
		for _, t := range strings.Split(v, ",") {
			if strings.EqualFold(strings.TrimSpace(t), token) {
				return true
			}
		}
	}
	return false
}

// SetLogger attaches a logger used for non-fatal connection errors.
func (c *Conn) SetLogger(l Logger) {
	if l == nil {
		l = discardLogger{}
	}
	c.logger = l
}

// ReadMessage blocks until the next complete data message, handling ping/pong
// and the closing handshake internally. Returns *CloseError on close.
func (c *Conn) ReadMessage() (opcode byte, payload []byte, err error) {
	buf := make([]byte, 4096)
	for {
		n, rerr := c.br.Read(buf)
		if n > 0 {
			// Feed may return already-decoded frames together with the error
			// that killed the following frame; process the good ones first.
			frames, ferr := c.parser.Feed(buf[:n])
			for _, f := range frames {
				ev, herr := c.reassembler.HandleFrame(f)
				if herr != nil {
					c.fail(CloseCode(herr), herr.Error())
					return 0, nil, herr
				}
				if ev == nil {
					continue // fragment accumulated; message not complete yet
				}
				switch ev.Type {
				case EventPing:
					if werr := c.writeControl(OpPong, ev.Payload); werr != nil {
						c.wc.Close()
						return 0, nil, werr
					}
				case EventPong:
					// Unsolicited pong; nothing to do.
				case EventClose:
					c.closeHandshake(ev)
					return 0, nil, &CloseError{Code: ev.CloseCode, Reason: ev.CloseReason, SentByPeer: true}
				case EventMessage:
					return ev.OpCode, ev.Payload, nil
				}
			}
			if ferr != nil {
				c.fail(CloseCode(ferr), ferr.Error())
				return 0, nil, ferr
			}
		}
		if rerr != nil {
			if errors.Is(rerr, io.EOF) {
				return 0, nil, &CloseError{Code: 1006, Reason: "connection closed without close frame", SentByPeer: true}
			}
			return 0, nil, rerr
		}
	}
}

// WriteMessage writes one un-fragmented, unmasked text or binary frame (server side).
func (c *Conn) WriteMessage(opcode byte, payload []byte) error {
	data, err := EncodeFrame(Frame{Fin: true, OpCode: opcode, Payload: payload}, false)
	if err != nil {
		return err
	}
	if c.writeDeadline > 0 {
		_ = c.wc.SetWriteDeadline(time.Now().Add(c.writeDeadline))
	}
	_, err = c.wc.Write(data)
	return err
}

func (c *Conn) writeControl(opcode byte, payload []byte) error {
	data, err := EncodeFrame(Frame{Fin: true, OpCode: opcode, Payload: payload}, false)
	if err != nil {
		return err
	}
	if c.writeDeadline > 0 {
		_ = c.wc.SetWriteDeadline(time.Now().Add(c.writeDeadline))
	}
	_, err = c.wc.Write(data)
	return err
}

// writeClose sends a close frame carrying the code and, optionally, a UTF-8 reason.
func (c *Conn) writeClose(code int, reason string) error {
	payload := encodeClosePayload(code, reason)
	return c.writeControl(OpClose, payload)
}

func encodeClosePayload(code int, reason string) []byte {
	if code == 0 {
		return nil
	}
	b := []byte{byte(code >> 8), byte(code)}
	return append(b, reason...)
}

// closeHandshake echoes the peer's close and drains input briefly (RFC 6455 §7.1.2).
func (c *Conn) closeHandshake(ev *Event) {
	if err := c.writeClose(ev.CloseCode, ev.CloseReason); err != nil {
		_ = c.wc.Close()
		return
	}
	_ = c.wc.SetReadDeadline(time.Now().Add(closeWaitTimeout))
	drain := make([]byte, 1024)
	for {
		if _, err := c.br.Read(drain); err != nil {
			break
		}
	}
	_ = c.wc.Close()
}

// fail sends a close frame for a protocol/policy error and tears down the TCP
// connection. The peer is not drained because the stream is invalid.
func (c *Conn) fail(code int, reason string) {
	if err := c.writeClose(code, ""); err != nil {
		c.logger.Printf("ws: writing close code=%d: %v", code, err)
	}
	_ = c.wc.Close()
}

// Serve is the default handler: it performs the upgrade and then echoes every
// complete text or binary message back verbatim.
func Serve(w http.ResponseWriter, r *http.Request, opts *Options) {
	if opts == nil {
		opts = &Options{}
	}
	conn, err := Upgrade(w, r, opts.MaxFrame)
	if err != nil {
		return // Upgrade already wrote the HTTP error response.
	}
	conn.SetLogger(opts.Logger)
	conn.reassembler = NewReassembler(ReassemblerConfig{MaxMessage: opts.MaxMessage, RequireMask: true})

	for {
		opcode, payload, rerr := conn.ReadMessage()
		if rerr != nil {
			var ce *CloseError
			if errors.As(rerr, &ce) {
				if opts.Logger != nil {
					opts.Logger.Printf("ws: %s: %v", r.RemoteAddr, ce)
				}
			} else if opts.Logger != nil {
				opts.Logger.Printf("ws: %s: read error: %v", r.RemoteAddr, rerr)
			}
			return
		}
		if werr := conn.WriteMessage(opcode, payload); werr != nil {
			if opts.Logger != nil {
				opts.Logger.Printf("ws: %s: write error: %v", r.RemoteAddr, werr)
			}
			return
		}
	}
}
