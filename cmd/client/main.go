// Command ws-client is a raw-socket WebSocket demo client (no third-party
// libraries). It performs the RFC 6455 handshake by hand, sends masked frames
// and exercises every acceptance scenario of the message-reassembly server:
// fragmentation, interleaved control frames, cross-fragment UTF-8 cases,
// protocol errors and oversized frames/messages.
package main

import (
	"bufio"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"

	"wsreassemble/ws"
)

type client struct {
	conn    net.Conn
	br      *bufio.Reader
	parser  *ws.FrameParser
	reasm   *ws.Reassembler
	pending []ws.Event // events decoded together but consumed one at a time
}

func dial(rawURL string) (*client, string, error) {
	u, err := url.Parse(rawURL)
	if err != nil {
		return nil, "", err
	}
	if u.Scheme != "ws" {
		return nil, "", fmt.Errorf("demo client only supports ws:// URLs, got %q", u.Scheme)
	}
	host := u.Host
	if !strings.Contains(host, ":") {
		host += ":80"
	}
	conn, err := net.DialTimeout("tcp", host, 5*time.Second)
	if err != nil {
		return nil, "", err
	}

	keyBytes := make([]byte, 16)
	if _, err := rand.Read(keyBytes); err != nil {
		conn.Close()
		return nil, "", err
	}
	key := base64.StdEncoding.EncodeToString(keyBytes)
	path := u.RequestURI()
	if path == "" {
		path = "/"
	}
	req := fmt.Sprintf("GET %s HTTP/1.1\r\n"+
		"Host: %s\r\n"+
		"Upgrade: websocket\r\n"+
		"Connection: Upgrade\r\n"+
		"Sec-WebSocket-Key: %s\r\n"+
		"Sec-WebSocket-Version: 13\r\n\r\n", path, u.Host, key)
	if _, err := conn.Write([]byte(req)); err != nil {
		conn.Close()
		return nil, "", err
	}

	br := bufio.NewReader(conn)
	resp, err := http.ReadResponse(br, &http.Request{Method: http.MethodGet})
	if err != nil {
		conn.Close()
		return nil, "", err
	}
	if resp.StatusCode != http.StatusSwitchingProtocols {
		conn.Close()
		return nil, "", fmt.Errorf("handshake failed: %s", resp.Status)
	}
	if got := resp.Header.Get("Sec-WebSocket-Accept"); got != ws.AcceptKey(key) {
		conn.Close()
		return nil, "", fmt.Errorf("bad Sec-WebSocket-Accept: got %q want %q", got, ws.AcceptKey(key))
	}

	c := &client{
		conn:   conn,
		br:     br,
		parser: ws.NewFrameParser(16 << 20),
		reasm:  ws.NewReassembler(ws.ReassemblerConfig{MaxMessage: 16 << 20}),
	}
	return c, key, nil
}

// send encodes one masked frame and writes it.
func (c *client) send(f ws.Frame) error {
	data, err := ws.EncodeFrame(f, true)
	if err != nil {
		return err
	}
	_, err = c.conn.Write(data)
	return err
}

// nextEvent returns the next message/close/pong event. Events produced by a
// single TCP read are queued (otherwise a coalesced "pong + echo" would lose
// the echo); pings are answered automatically and never queued.
func (c *client) nextEvent() (*ws.Event, error) {
	if len(c.pending) > 0 {
		ev := c.pending[0]
		c.pending = c.pending[1:]
		return &ev, nil
	}
	buf := make([]byte, 4096)
	for {
		n, rerr := c.br.Read(buf)
		if n > 0 {
			frames, ferr := c.parser.Feed(buf[:n])
			if ferr != nil {
				return nil, fmt.Errorf("frame error: %w", ferr)
			}
			for _, f := range frames {
				ev, herr := c.reasm.HandleFrame(f)
				if herr != nil {
					return nil, fmt.Errorf("protocol error (close code %d): %w", ws.CloseCode(herr), herr)
				}
				if ev == nil {
					continue
				}
				if ev.Type == ws.EventPing {
					_ = c.send(ws.Frame{Fin: true, OpCode: ws.OpPong, Payload: ev.Payload})
					continue
				}
				c.pending = append(c.pending, *ev)
			}
			if len(c.pending) > 0 {
				return c.nextEvent()
			}
		}
		if rerr != nil {
			if errors.Is(rerr, io.EOF) {
				return nil, io.EOF
			}
			return nil, rerr
		}
	}
}

func (c *client) expectEcho(wantOpcode byte, want []byte) bool {
	ev, err := c.nextEvent()
	if err != nil {
		log.Printf("    FAIL expected echo, got error: %v", err)
		return false
	}
	if ev.Type == ws.EventClose {
		log.Printf("    FAIL expected echo, got close code=%d", ev.CloseCode)
		return false
	}
	if ev.OpCode != wantOpcode || !bytesEqual(ev.Payload, want) {
		log.Printf("    FAIL echo mismatch: opcode=%d len=%d, want opcode=%d len=%d",
			ev.OpCode, len(ev.Payload), wantOpcode, len(want))
		return false
	}
	return true
}

// expectClose reads until a close frame or EOF and reports the close code.
func (c *client) expectClose() int {
	ev, err := c.nextEvent()
	if err != nil {
		log.Printf("    (connection ended: %v)", err)
		return 1006
	}
	if ev.Type != ws.EventClose {
		log.Printf("    FAIL expected close, got event type %d", ev.Type)
		return -1
	}
	log.Printf("    server closed: code=%d reason=%q", ev.CloseCode, ev.CloseReason)
	return ev.CloseCode
}

func bytesEqual(a, b []byte) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func run(rawURL string, maxFrame, maxMsg int) bool {
	ok := true
	check := func(name string, passed bool) {
		if passed {
			log.Printf("PASS %s", name)
		} else {
			log.Printf("PASS %s -- FAILED", name)
			ok = false
		}
	}

	// Scenario 1: single text message.
	c1, _, err := dial(rawURL)
	if err != nil {
		log.Fatalf("dial: %v", err)
	}
	if err := c1.send(ws.Frame{Fin: true, OpCode: ws.OpText, Payload: []byte("hello world")}); err != nil {
		log.Fatal(err)
	}
	check("1. single text message echo", c1.expectEcho(ws.OpText, []byte("hello world")))

	// Scenario 2: fragmented text with an interleaved ping between fragments.
	// "你好" = E4 BD A0 E5 A5 BD (two 3-byte UTF-8 sequences); split exactly
	// across a continuation boundary.
	if err := c1.send(ws.Frame{Fin: false, OpCode: ws.OpText, Payload: []byte("ab-")}); err != nil {
		log.Fatal(err)
	}
	if err := c1.send(ws.Frame{Fin: true, OpCode: ws.OpPing, Payload: []byte("knock")}); err != nil {
		log.Fatal(err)
	}
	if !c1.expectEcho(ws.OpPong, []byte("knock")) {
		log.Printf("    FAIL: ping was not answered with pong")
		ok = false
	}
	if err := c1.send(ws.Frame{Fin: false, OpCode: ws.OpContinuation, Payload: []byte{0xE4, 0xBD}}); err != nil {
		log.Fatal(err)
	}
	if err := c1.send(ws.Frame{Fin: true, OpCode: ws.OpContinuation, Payload: []byte{0xA0, 0xE5, 0xA5, 0xBD, '-', 'z'}}); err != nil {
		log.Fatal(err)
	}
	want2 := []byte("ab-\xE4\xBD\xA0\xE5\xA5\xBD-z")
	check("2. fragmented text (ping interleaved, multibyte char split)", c1.expectEcho(ws.OpText, want2))

	// Scenario 3: fragmented binary.
	bin := []byte{0x00, 0x01, 0xFE, 0xFF, 0x10, 0x20}
	if err := c1.send(ws.Frame{Fin: false, OpCode: ws.OpBinary, Payload: bin[:2]}); err != nil {
		log.Fatal(err)
	}
	if err := c1.send(ws.Frame{Fin: true, OpCode: ws.OpContinuation, Payload: bin[2:]}); err != nil {
		log.Fatal(err)
	}
	check("3. fragmented binary echo", c1.expectEcho(ws.OpBinary, bin))
	_ = c1.conn.Close()

	// Scenario 4: half of a multibyte char inside one fragment, then the other
	// half — VALID across the fragment boundary.
	c2, _, err := dial(rawURL)
	if err != nil {
		log.Fatalf("dial: %v", err)
	}
	if err := c2.send(ws.Frame{Fin: false, OpCode: ws.OpText, Payload: []byte("x=")}); err != nil {
		log.Fatal(err)
	}
	if err := c2.send(ws.Frame{Fin: false, OpCode: ws.OpContinuation, Payload: []byte{0xE4, 0xBD}}); err != nil {
		log.Fatal(err)
	}
	if err := c2.send(ws.Frame{Fin: true, OpCode: ws.OpContinuation, Payload: []byte{0xA0}}); err != nil {
		log.Fatal(err)
	}
	check("4. half multibyte char split across fragments is valid UTF-8",
		c2.expectEcho(ws.OpText, []byte("x=\xE4\xBD\xA0")))

	// Scenario 5: invalid UTF-8 inside a continuation frame -> close 1007.
	if err := c2.send(ws.Frame{Fin: false, OpCode: ws.OpText, Payload: []byte("ok:")}); err != nil {
		log.Fatal(err)
	}
	if err := c2.send(ws.Frame{Fin: true, OpCode: ws.OpContinuation, Payload: []byte{0xFF, 0xFE}}); err != nil {
		log.Fatal(err)
	}
	check("5. invalid continuation bytes rejected (expect 1007)", c2.expectClose() == 1007)
	_ = c2.conn.Close()

	// Scenario 6: dangling lead byte at message end -> close 1007.
	c3, _, err := dial(rawURL)
	if err != nil {
		log.Fatalf("dial: %v", err)
	}
	if err := c3.send(ws.Frame{Fin: false, OpCode: ws.OpText, Payload: []byte("hi-")}); err != nil {
		log.Fatal(err)
	}
	if err := c3.send(ws.Frame{Fin: true, OpCode: ws.OpContinuation, Payload: []byte{0xE4, 0xBD}}); err != nil {
		log.Fatal(err)
	}
	check("6. dangling half-character at end rejected (expect 1007)", c3.expectClose() == 1007)
	_ = c3.conn.Close()

	// Scenario 7: continuation frame without a start frame -> close 1002.
	c4, _, err := dial(rawURL)
	if err != nil {
		log.Fatalf("dial: %v", err)
	}
	if err := c4.send(ws.Frame{Fin: true, OpCode: ws.OpContinuation, Payload: []byte("x")}); err != nil {
		log.Fatal(err)
	}
	check("7. unexpected continuation frame rejected (expect 1002)", c4.expectClose() == 1002)
	_ = c4.conn.Close()

	// Scenario 8: fragmented control frame (Fin=0 ping) -> close 1002. Such a
	// frame is illegal to *encode*, so craft the raw wire bytes by hand.
	c5, _, err := dial(rawURL)
	if err != nil {
		log.Fatalf("dial: %v", err)
	}
	if _, err := c5.conn.Write([]byte{0x09, 0x81, 0x11, 0x22, 0x33, 0x44, 'x' ^ 0x11}); err != nil {
		log.Fatal(err)
	}
	check("8. fragmented control frame rejected (expect 1002)", c5.expectClose() == 1002)
	_ = c5.conn.Close()

	// Scenario 9: frame larger than the frame limit -> close 1009.
	c6, _, err := dial(rawURL)
	if err != nil {
		log.Fatalf("dial: %v", err)
	}
	big := make([]byte, maxFrame+1)
	// The server may send its 1009 close and RST immediately after parsing the
	// oversized header, before the payload lands; a write reset is expected.
	if err := c6.send(ws.Frame{Fin: true, OpCode: ws.OpBinary, Payload: big}); err != nil {
		log.Printf("    (write after oversize rejected as expected: %v)", err)
	}
	check(fmt.Sprintf("9. oversized single frame rejected (expect 1009, limit=%d)", maxFrame),
		c6.expectClose() == 1009)
	_ = c6.conn.Close()

	// Scenario 10: message larger than the message limit, sent in fragments
	// that stay under the per-frame limit -> must be rejected at 1009 anyway.
	c7, _, err := dial(rawURL)
	if err != nil {
		log.Fatalf("dial: %v", err)
	}
	chunk := maxFrame - 1
	nSent := 0
	first := true
	var writeErr error
	for nSent <= maxMsg && writeErr == nil {
		payload := make([]byte, chunk)
		if first {
			writeErr = c7.send(ws.Frame{Fin: false, OpCode: ws.OpBinary, Payload: payload})
			first = false
		} else {
			// Keep the message open; the server must enforce the aggregate cap.
			writeErr = c7.send(ws.Frame{Fin: false, OpCode: ws.OpContinuation, Payload: payload})
		}
		nSent += chunk
	}
	if writeErr != nil {
		log.Printf("    (write after oversize rejected as expected: %v)", writeErr)
	}
	// The 1009 close typically arrives before (or when) the final frame lands;
	// drain it immediately rather than sending more data on a dead socket.
	check(fmt.Sprintf("10. oversized fragmented message rejected (expect 1009, msg limit=%d)", maxMsg),
		c7.expectClose() == 1009)
	_ = c7.conn.Close()

	// Scenario 11: clean closing handshake initiated by client.
	c8, _, err := dial(rawURL)
	if err != nil {
		log.Fatalf("dial: %v", err)
	}
	if err := c8.send(ws.Frame{Fin: true, OpCode: ws.OpText, Payload: []byte("bye")}); err != nil {
		log.Fatal(err)
	}
	check("11a. final echo before close", c8.expectEcho(ws.OpText, []byte("bye")))
	closeBody := []byte{0x03, 0xE8} // 1000 going away
	closeBody = append(closeBody, []byte("goodbye")...)
	if err := c8.send(ws.Frame{Fin: true, OpCode: ws.OpClose, Payload: closeBody}); err != nil {
		log.Fatal(err)
	}
	check("11b. closing handshake echoed (expect 1000)", c8.expectClose() == 1000)
	_ = c8.conn.Close()

	return ok
}

func main() {
	urlFlag := flag.String("url", "ws://127.0.0.1:8080/ws", "WebSocket server URL")
	maxFrameFlag := flag.Int("max-frame", 1<<20, "server's --max-frame value (bytes), used to size the oversize test")
	maxMsgFlag := flag.Int("max-message", 4<<20, "server's --max-message value (bytes)")
	flag.Parse()

	log.SetFlags(log.Ltime | log.Lmicroseconds)
	log.Printf("connecting to %s", *urlFlag)
	if run(*urlFlag, *maxFrameFlag, *maxMsgFlag) {
		log.Printf("ALL SCENARIOS PASSED")
	} else {
		log.Fatalf("SOME SCENARIOS FAILED")
	}
}
