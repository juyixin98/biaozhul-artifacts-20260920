package ws_test

import (
	"bufio"
	"crypto/rand"
	"encoding/base64"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"wsreassemble/ws"
)

// testConn is a minimal raw WebSocket peer used to drive the real server.
type testConn struct {
	conn net.Conn
	br   *bufio.Reader
	p    *ws.FrameParser
	r    *ws.Reassembler
	// pending holds events decoded from one TCP read but not yet consumed;
	// without it a coalesced "pong + message" read would drop the second.
	pending []ws.Event
}

func handshake(t *testing.T, rawURL string) *testConn {
	t.Helper()
	u := strings.TrimPrefix(rawURL, "http://")
	conn, err := net.DialTimeout("tcp", u, 5*time.Second)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	keyBytes := make([]byte, 16)
	if _, err := rand.Read(keyBytes); err != nil {
		t.Fatal(err)
	}
	key := base64.StdEncoding.EncodeToString(keyBytes)
	req := "GET /ws HTTP/1.1\r\n" +
		"Host: " + u + "\r\n" +
		"Upgrade: websocket\r\n" +
		"Connection: Upgrade\r\n" +
		"Sec-WebSocket-Key: " + key + "\r\n" +
		"Sec-WebSocket-Version: 13\r\n\r\n"
	if _, err := conn.Write([]byte(req)); err != nil {
		t.Fatal(err)
	}
	br := bufio.NewReader(conn)
	resp, err := http.ReadResponse(br, nil)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != 101 {
		t.Fatalf("status = %d, want 101", resp.StatusCode)
	}
	if resp.Header.Get("Sec-WebSocket-Accept") != ws.AcceptKey(key) {
		t.Fatal("bad accept key")
	}
	return &testConn{
		conn: conn, br: br,
		p: ws.NewFrameParser(0),
		r: ws.NewReassembler(ws.ReassemblerConfig{}),
	}
}

func (c *testConn) send(f ws.Frame) {
	data, err := ws.EncodeFrame(f, true)
	if err != nil {
		panic(err)
	}
	if _, err := c.conn.Write(data); err != nil {
		panic(err)
	}
}

func (c *testConn) sendRaw(b []byte) {
	if _, err := c.conn.Write(b); err != nil {
		panic(err)
	}
}

// queueAll decodes every frame in bytes into reassembler events and appends
// them to the pending queue. Frames that only accumulate a fragment produce
// no event and are simply skipped.
func (c *testConn) queueAll(bytes []byte, p *ws.FrameParser, reasm *ws.Reassembler) bool {
	frames, err := p.Feed(bytes)
	if err != nil {
		panic(err)
	}
	for _, f := range frames {
		ev, err := reasm.HandleFrame(f)
		if err != nil {
			panic(err)
		}
		if ev != nil {
			c.pending = append(c.pending, *ev)
		}
	}
	return len(c.pending) > 0
}

func (c *testConn) nextEvent() *ws.Event {
	if len(c.pending) > 0 {
		ev := c.pending[0]
		c.pending = c.pending[1:]
		return &ev
	}
	buf := make([]byte, 4096)
	for {
		n, err := c.br.Read(buf)
		if n > 0 {
			if c.queueAll(buf[:n], c.p, c.r) {
				return c.nextEvent()
			}
		}
		if err != nil {
			if err == io.EOF {
				return nil
			}
			panic(err)
		}
	}
}

// readCloseCode reads until a close frame is queued, leaving any other events
// (pongs, messages) pending for later checks.
func (c *testConn) readCloseCode() (int, bool) {
	for {
		for i := range c.pending {
			if c.pending[i].Type == ws.EventClose {
				code := c.pending[i].CloseCode
				c.pending = append(c.pending[:i], c.pending[i+1:]...)
				return code, true
			}
		}
		buf := make([]byte, 4096)
		n, err := c.br.Read(buf)
		if n > 0 {
			frames, ferr := c.p.Feed(buf[:n])
			if ferr != nil {
				return -1, false
			}
			for _, f := range frames {
				ev, herr := c.r.HandleFrame(f)
				if herr != nil {
					return -1, false
				}
				if ev != nil {
					c.pending = append(c.pending, *ev)
				}
			}
		}
		if err != nil {
			return -1, false
		}
	}
}

func newTestServer(t *testing.T, opts *ws.Options) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("/ws", func(w http.ResponseWriter, r *http.Request) {
		ws.Serve(w, r, opts)
	})
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, `{"status":"ok"}`)
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv
}

func TestEndToEndEchoFragmentedWithPing(t *testing.T) {
	srv := newTestServer(t, &ws.Options{MaxFrame: 1 << 20, MaxMessage: 4 << 20})
	c := handshake(t, srv.URL)
	defer c.conn.Close()

	c.send(ws.Frame{Fin: false, OpCode: ws.OpText, Payload: []byte("ab-")})
	c.send(ws.Frame{Fin: true, OpCode: ws.OpPing, Payload: []byte("knock")})
	c.send(ws.Frame{Fin: false, OpCode: ws.OpContinuation, Payload: []byte{0xE4, 0xBD}})
	c.send(ws.Frame{Fin: true, OpCode: ws.OpContinuation, Payload: []byte{0xA0, 'z'}})

	// First server event must be the pong.
	ev := c.nextEvent()
	if ev.Type != ws.EventPong || string(ev.Payload) != "knock" {
		t.Fatalf("expected pong, got %+v", ev)
	}
	ev = c.nextEvent()
	want := []byte("ab-\xE4\xBD\xA0z")
	if ev.Type != ws.EventMessage || string(ev.Payload) != string(want) {
		t.Fatalf("echo = %q, want %q", ev.Payload, want)
	}
}

func TestEndToEndBadUTF8(t *testing.T) {
	srv := newTestServer(t, &ws.Options{})
	c := handshake(t, srv.URL)
	defer c.conn.Close()

	c.send(ws.Frame{Fin: false, OpCode: ws.OpText, Payload: []byte("ok:")})
	c.send(ws.Frame{Fin: true, OpCode: ws.OpContinuation, Payload: []byte{0xFF}})
	code, ok := c.readCloseCode()
	if !ok || code != 1007 {
		t.Fatalf("close code = %d ok=%v, want 1007", code, ok)
	}
}

func TestEndToEndUnexpectedContinuation(t *testing.T) {
	srv := newTestServer(t, &ws.Options{})
	c := handshake(t, srv.URL)
	defer c.conn.Close()

	c.send(ws.Frame{Fin: true, OpCode: ws.OpContinuation, Payload: []byte("x")})
	code, ok := c.readCloseCode()
	if !ok || code != 1002 {
		t.Fatalf("close code = %d ok=%v, want 1002", code, ok)
	}
}

func TestEndToEndFragmentedControl(t *testing.T) {
	srv := newTestServer(t, &ws.Options{})
	c := handshake(t, srv.URL)
	defer c.conn.Close()

	c.sendRaw([]byte{0x09, 0x81, 0x00, 0x00, 0x00, 0x00, 'x'}) // FIN=0 ping, len 1, masked
	code, _ := c.readCloseCode()
	if code != 1002 {
		t.Fatalf("close code = %d, want 1002", code)
	}
}

func TestEndToEndOversized(t *testing.T) {
	srv := newTestServer(t, &ws.Options{MaxFrame: 1000, MaxMessage: 3000})

	// Oversized single frame -> 1009.
	c1 := handshake(t, srv.URL)
	c1.send(ws.Frame{Fin: true, OpCode: ws.OpBinary, Payload: make([]byte, 1001)})
	if code, _ := c1.readCloseCode(); code != 1009 {
		t.Fatalf("oversized frame: close code = %d, want 1009", code)
	}
	c1.conn.Close()

	// Oversized message assembled from legal frames -> 1009. Four fragments of
	// 900 bytes (3600 total) exceed the 3000 message limit while each frame
	// stays under the 1000 per-frame limit.
	c2 := handshake(t, srv.URL)
	c2.send(ws.Frame{Fin: false, OpCode: ws.OpBinary, Payload: make([]byte, 900)})
	c2.send(ws.Frame{Fin: false, OpCode: ws.OpContinuation, Payload: make([]byte, 900)})
	c2.send(ws.Frame{Fin: false, OpCode: ws.OpContinuation, Payload: make([]byte, 900)})
	c2.send(ws.Frame{Fin: true, OpCode: ws.OpContinuation, Payload: make([]byte, 900)})
	if code, _ := c2.readCloseCode(); code != 1009 {
		t.Fatalf("oversized message: close code = %d, want 1009", code)
	}
	c2.conn.Close()
}

func TestEndToEndUnmaskedFrame(t *testing.T) {
	srv := newTestServer(t, &ws.Options{})
	c := handshake(t, srv.URL)
	defer c.conn.Close()

	raw, err := ws.EncodeFrame(ws.Frame{Fin: true, OpCode: ws.OpText, Payload: []byte("x")}, false)
	if err != nil {
		t.Fatal(err)
	}
	c.sendRaw(raw)
	code, _ := c.readCloseCode()
	if code != 1002 {
		t.Fatalf("close code = %d, want 1002", code)
	}
}

func TestEndToEndCloseHandshake(t *testing.T) {
	srv := newTestServer(t, &ws.Options{})
	c := handshake(t, srv.URL)
	defer c.conn.Close()

	c.send(ws.Frame{Fin: true, OpCode: ws.OpText, Payload: []byte("bye")})
	if ev := c.nextEvent(); string(ev.Payload) != "bye" {
		t.Fatalf("echo = %q", ev.Payload)
	}
	c.send(ws.Frame{Fin: true, OpCode: ws.OpClose, Payload: []byte{0x03, 0xE8}})
	code, ok := c.readCloseCode()
	if !ok || code != 1000 {
		t.Fatalf("close code = %d ok=%v, want echoed 1000", code, ok)
	}
}

// TestEndToEndRandomChunks writes one message's frames byte-at-a-time with
// TCP_NODELAY; regardless of how the kernel coalesces them, the echo must be
// the complete reassembled message.
func TestEndToEndRandomChunks(t *testing.T) {
	srv := newTestServer(t, &ws.Options{MaxFrame: 1 << 20, MaxMessage: 4 << 20})
	c := handshake(t, srv.URL)
	defer c.conn.Close()
	if tc, ok := c.conn.(*net.TCPConn); ok {
		_ = tc.SetNoDelay(true)
	}

	payload := []byte("chunked-delivery-你好-binary")
	wire, err := ws.EncodeFrame(ws.Frame{Fin: false, OpCode: ws.OpText, Payload: payload[:5]}, true)
	if err != nil {
		t.Fatal(err)
	}
	rest, err := ws.EncodeFrame(ws.Frame{Fin: true, OpCode: ws.OpContinuation, Payload: payload[5:]}, true)
	if err != nil {
		t.Fatal(err)
	}
	wire = append(wire, rest...)

	// Write one byte at a time, with tiny pauses, to force segmentation.
	for i, b := range wire {
		if _, err := c.conn.Write([]byte{b}); err != nil {
			t.Fatalf("write byte %d: %v", i, err)
		}
		time.Sleep(time.Millisecond)
	}
	ev := c.nextEvent()
	if ev == nil || string(ev.Payload) != string(payload) {
		t.Fatalf("echo = %q, want %q", ev, payload)
	}
}

func TestHealthAndRoot(t *testing.T) {
	srv := newTestServer(t, &ws.Options{})
	resp, err := http.Get(srv.URL + "/health")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("health status = %d", resp.StatusCode)
	}
	body, _ := io.ReadAll(resp.Body)
	if !strings.Contains(string(body), "ok") {
		t.Fatalf("health body = %q", body)
	}
}

func TestHandshakeFailures(t *testing.T) {
	srv := newTestServer(t, &ws.Options{})

	// Non-GET.
	resp, err := http.Post(srv.URL+"/ws", "text/plain", strings.NewReader("x"))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("POST status = %d, want 405", resp.StatusCode)
	}

	// GET without upgrade headers.
	resp2, err := http.Get(srv.URL + "/ws")
	if err != nil {
		t.Fatal(err)
	}
	resp2.Body.Close()
	if resp2.StatusCode != http.StatusBadRequest {
		t.Fatalf("plain GET status = %d, want 400", resp2.StatusCode)
	}
}
