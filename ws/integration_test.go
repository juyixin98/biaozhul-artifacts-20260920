package ws_test

import (
	"bufio"
	"bytes"
	cryptorand "crypto/rand"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"wsreassemble/ws"
)

// startEchoServer 启动一个真实 net/http 服务，走完整握手 + echo 循环。
func startEchoServer(t *testing.T, maxFrame, maxMsg int64) (*httptest.Server, ws.Upgrader) {
	t.Helper()
	up := ws.Upgrader{MaxFrameSize: maxFrame, MaxMessageSize: maxMsg}
	mux := http.NewServeMux()
	mux.HandleFunc("/ws", func(w http.ResponseWriter, r *http.Request) {
		conn, err := up.Upgrade(w, r)
		if err != nil {
			return
		}
		defer conn.Close()
		for {
			events, err := conn.ReadEvents()
			terminate := false
			for _, ev := range events {
				switch ev.Kind {
				case "message":
					_ = conn.WriteMessage(ev.OpCode, ev.Data)
				case "ping":
					_ = conn.WritePong(ev.Data)
				case "close":
					code := ws.CloseCode(ev.Data)
					if code == 0 {
						code = ws.CloseNormalClosure
					}
					_ = conn.SendClose(code, "")
					terminate = true
				}
			}
			if terminate {
				return
			}
			if err != nil {
				// 与 cmd/wsserver 一致：协议错误时回带关闭码的 Close 帧。
				var fe *ws.FrameError
				if errors.As(err, &fe) {
					_ = conn.SendClose(fe.Code, "")
				}
				return
			}
		}
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv, up
}

func dial(t *testing.T, srv *httptest.Server) (net.Conn, *bufio.Reader, *ws.Decoder, *ws.FrameWriter) {
	t.Helper()
	u := strings.Replace(srv.URL, "http://", "ws://", 1) + "/ws"
	_ = u
	conn, err := net.DialTimeout("tcp", strings.TrimPrefix(srv.URL, "http://"), 5*time.Second)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	t.Cleanup(func() { conn.Close() })
	br := bufio.NewReader(conn)

	keyRaw := make([]byte, 16)
	_, _ = cryptorand.Read(keyRaw)
	key := base64.StdEncoding.EncodeToString(keyRaw)
	fmt.Fprintf(conn, "GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n", key)
	resp, err := http.ReadResponse(br, nil)
	if err != nil {
		t.Fatalf("handshake read: %v", err)
	}
	if resp.StatusCode != http.StatusSwitchingProtocols {
		t.Fatalf("status = %s", resp.Status)
	}
	if got := resp.Header.Get("Sec-WebSocket-Accept"); got != ws.AcceptKey(key) {
		t.Fatalf("accept = %q", got)
	}
	return conn, br, &ws.Decoder{RequireMask: false}, &ws.FrameWriter{W: conn, Mask: true}
}

func drainEvents(t *testing.T, dec *ws.Decoder, br *bufio.Reader) []ws.Event {
	t.Helper()
	buf := make([]byte, 64*1024)
	n, err := br.Read(buf)
	if err != nil && !(errors.Is(err, io.EOF) && n > 0) {
		t.Fatalf("read: %v", err)
	}
	evs, ferr := dec.Feed(buf[:n])
	if ferr != nil {
		t.Fatalf("server frame error: %v", ferr)
	}
	return evs
}

func TestEndToEndFragmentedWithPing(t *testing.T) {
	srv, _ := startEchoServer(t, ws.DefaultMaxFrameSize, ws.DefaultMaxMessageSize)
	conn, br, dec, fw := dial(t, srv)
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))

	// 把 "你好, world" 拆成 3 字节片（必然在 3 字节汉字中间切断），
	// 中间穿插 ping。
	msg := []byte("你好, world")
	chunks := chunkBytes(msg, 3)
	for i, ch := range chunks {
		fin := i == len(chunks)-1
		if i == 0 {
			if err := fw.WriteFrame(fin, ws.OpText, ch); err != nil {
				t.Fatal(err)
			}
		} else {
			if err := fw.WriteFrame(fin, ws.OpContinuation, ch); err != nil {
				t.Fatal(err)
			}
		}
		if !fin && i == 1 {
			if err := fw.WritePing([]byte("hb")); err != nil {
				t.Fatal(err)
			}
		}
	}

	// 先应收到 pong，再收到重组后的整条 echo。
	var got []ws.Event
	for {
		evs := drainEvents(t, dec, br)
		got = append(got, evs...)
		if hasMessage(got) {
			break
		}
	}
	var pong, message *ws.Event
	for i := range got {
		switch got[i].Kind {
		case "pong":
			pong = &got[i]
		case "message":
			message = &got[i]
		}
	}
	if pong == nil || string(pong.Data) != "hb" {
		t.Fatalf("missing pong: %+v", got)
	}
	if message == nil || message.OpCode != ws.OpText || string(message.Data) != string(msg) {
		t.Fatalf("echo mismatch: %+v", message)
	}

	// 关闭握手。
	if err := fw.WriteClose(ws.CloseNormalClosure, ""); err != nil {
		t.Fatal(err)
	}
	var closeAck []ws.Event
	for {
		evs := drainEvents(t, dec, br)
		closeAck = append(closeAck, evs...)
		if hasClose(closeAck) {
			break
		}
	}
	if len(closeAck) != 1 || closeAck[0].Kind != "close" ||
		ws.CloseCode(closeAck[0].Data) != ws.CloseNormalClosure {
		t.Fatalf("close ack = %+v", closeAck)
	}
}

func TestEndToEndOversizedMessageRejected(t *testing.T) {
	srv, _ := startEchoServer(t, 128, 256)
	conn, br, dec, fw := dial(t, srv)
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))

	if err := fw.WriteMessage(ws.OpBinary, make([]byte, 257)); err != nil {
		t.Fatal(err)
	}
	// 服务端可能把关闭帧拆在多个 TCP 段里，循环读到完整事件或对端关闭。
	dec.RequireMask = false
	var closeEv *ws.Event
	for closeEv == nil {
		buf := make([]byte, 4096)
		n, err := br.Read(buf)
		if n > 0 {
			evs, ferr := dec.Feed(buf[:n])
			if ferr != nil {
				t.Fatalf("decoding server close: %v", ferr)
			}
			for i := range evs {
				if evs[i].Kind == "close" {
					closeEv = &evs[i]
				}
			}
		}
		if err != nil {
			break
		}
	}
	if closeEv == nil {
		t.Fatal("no close frame received for oversized message")
	}
	if code := ws.CloseCode(closeEv.Data); code != ws.CloseMessageTooLarge {
		t.Fatalf("close code = %d, want 1009", code)
	}
}

func TestEndToEndBadHandshake(t *testing.T) {
	srv, _ := startEchoServer(t, 0, 0)
	resp, err := http.Get(srv.URL + "/ws")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("plain GET status = %d", resp.StatusCode)
	}
}

func TestEndToEndUnmaskedFrameRejected(t *testing.T) {
	srv, _ := startEchoServer(t, 0, 0)
	conn, br, _, _ := dial(t, srv)
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))

	// 服务端要求掩码；直接发一个未掩码文本帧（0x81 0x02 "hi"）。
	conn.Write([]byte{0x81, 0x02, 'h', 'i'})
	// 服务端回的帧不掩码，用客户端侧解码器循环读到关闭事件。
	dec := ws.NewDecoder()
	dec.RequireMask = false
	var saw1002 bool
	for !saw1002 {
		buf := make([]byte, 4096)
		n, err := br.Read(buf)
		if n > 0 {
			evs, ferr := dec.Feed(buf[:n])
			if ferr != nil {
				t.Fatalf("decoding server close: %v", ferr)
			}
			for _, ev := range evs {
				if ev.Kind == "close" && ws.CloseCode(ev.Data) == ws.CloseProtocolError {
					saw1002 = true
				}
			}
		}
		if err != nil {
			break
		}
	}
	if !saw1002 {
		t.Fatal("expected 1002 close frame after unmasked send")
	}
}

func chunkBytes(data []byte, size int) [][]byte {
	var out [][]byte
	for off := 0; off < len(data); off += size {
		end := off + size
		if end > len(data) {
			end = len(data)
		}
		out = append(out, data[off:end])
	}
	return out
}

func hasMessage(evs []ws.Event) bool {
	for _, ev := range evs {
		if ev.Kind == "message" {
			return true
		}
	}
	return false
}

func hasClose(evs []ws.Event) bool {
	for _, ev := range evs {
		if ev.Kind == "close" {
			return true
		}
	}
	return false
}

// ---- FrameWriter <-> Decoder 往返（掩码/不掩码、分片、长度档位） ----

func TestWriterDecoderRoundTrip(t *testing.T) {
	cases := [][]byte{
		nil,
		[]byte("x"),
		bytes.Repeat([]byte("A"), 125),
		bytes.Repeat([]byte("B"), 126),
		bytes.Repeat([]byte("C"), 65535),
		bytes.Repeat([]byte("D"), 65536),
	}
	for _, masked := range []bool{false, true} {
		for ci, payload := range cases {
			var buf bytes.Buffer
			fw := &ws.FrameWriter{W: &buf, Mask: masked}
			if err := fw.WriteMessage(ws.OpBinary, payload); err != nil {
				t.Fatalf("case %d masked=%v: %v", ci, masked, err)
			}
			dec := &ws.Decoder{
				RequireMask:    masked,
				MaxFrameSize:   0,
				MaxMessageSize: 0,
			}
			evs, err := dec.Feed(buf.Bytes())
			if err != nil {
				t.Fatalf("case %d masked=%v decode: %v", ci, masked, err)
			}
			if len(evs) != 1 || !bytes.Equal(evs[0].Data, payload) {
				t.Fatalf("case %d masked=%v mismatch", ci, masked)
			}
		}
	}
}

func TestWriterRejectsInvalidControl(t *testing.T) {
	fw := &ws.FrameWriter{W: io.Discard}
	if err := fw.WriteFrame(false, ws.OpPing, []byte("x")); !errors.Is(err, ws.ErrInvalidFrame) {
		t.Fatalf("fragmented control: %v", err)
	}
	if err := fw.WriteFrame(true, ws.OpPing, make([]byte, 126)); !errors.Is(err, ws.ErrInvalidFrame) {
		t.Fatalf("oversized control: %v", err)
	}
	fw.MaxMessageSize = 10
	if err := fw.WriteMessage(ws.OpText, make([]byte, 11)); !errors.Is(err, ws.ErrTooLarge) {
		t.Fatalf("oversized message: %v", err)
	}
}
