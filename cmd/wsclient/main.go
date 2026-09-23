// 命令 wsclient 是一个零依赖的 WebSocket 演示客户端：
// 裸 TCP 完成握手、用 ws.FrameWriter 发送掩码帧（可强制分片、可穿插 Ping），
// 再用 ws.Decoder 解析服务端响应，最后完成关闭握手。
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

func main() {
	rawURL := flag.String("url", "ws://127.0.0.1:8080/ws", "WebSocket 地址")
	msg := flag.String("msg", "你好, WebSocket!", "要发送的消息文本")
	binaryMode := flag.Bool("binary", false, "按二进制帧发送")
	frag := flag.Int("frag", 0, "把消息按该字节数分片发送（0=单个完整帧）")
	ping := flag.Bool("ping", false, "在分片间隙插入一个 Ping 帧")
	timeout := flag.Duration("timeout", 5*time.Second, "整体读写超时")
	flag.Parse()

	u, err := url.Parse(*rawURL)
	if err != nil {
		log.Fatal(err)
	}
	if u.Scheme != "ws" {
		log.Fatalf("仅支持 ws://（明文），收到 %q", u.Scheme)
	}
	host := u.Host
	if !strings.Contains(host, ":") {
		host += ":80"
	}

	conn, err := net.DialTimeout("tcp", host, *timeout)
	if err != nil {
		log.Fatalf("dial: %v", err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(*timeout))

	fw := &ws.FrameWriter{W: conn, Mask: true}
	dec := &ws.Decoder{RequireMask: false}
	br := bufio.NewReader(conn)

	if err := handshake(conn, br, u); err != nil {
		log.Fatalf("handshake: %v", err)
	}
	log.Printf("handshake ok: %s", *rawURL)

	opcode := ws.OpText
	payload := []byte(*msg)
	if *binaryMode {
		opcode = ws.OpBinary
	}
	if err := sendMessage(fw, opcode, payload, *frag, *ping); err != nil {
		log.Fatalf("send: %v", err)
	}
	log.Printf("sent message opcode=%d len=%d frag=%d ping=%v", opcode, len(payload), *frag, *ping)

	// 读取响应直到拿到 echo（与可能的 pong）。
	if err := pump(dec, br); err != nil {
		log.Fatalf("recv: %v", err)
	}

	// 关闭握手：发送 1000，等待对端 Close。
	if err := fw.WriteClose(ws.CloseNormalClosure, "bye"); err != nil {
		log.Fatalf("write close: %v", err)
	}
	if err := pump(dec, br); err != nil && !errors.Is(err, io.EOF) {
		log.Printf("waiting close ack: %v", err)
	}
}

func handshake(w io.Writer, br *bufio.Reader, u *url.URL) error {
	keyBytes := make([]byte, 16)
	if _, err := rand.Read(keyBytes); err != nil {
		return err
	}
	key := base64.StdEncoding.EncodeToString(keyBytes)
	path := u.RequestURI()
	if path == "" {
		path = "/"
	}
	req := "GET " + path + " HTTP/1.1\r\n" +
		"Host: " + u.Host + "\r\n" +
		"Upgrade: websocket\r\n" +
		"Connection: Upgrade\r\n" +
		"Sec-WebSocket-Key: " + key + "\r\n" +
		"Sec-WebSocket-Version: 13\r\n\r\n"
	if _, err := io.WriteString(w, req); err != nil {
		return err
	}

	resp, err := http.ReadResponse(br, nil)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusSwitchingProtocols {
		return fmt.Errorf("unexpected status %s", resp.Status)
	}
	want := ws.AcceptKey(key)
	if resp.Header.Get("Sec-WebSocket-Accept") != want {
		return fmt.Errorf("Sec-WebSocket-Accept mismatch: got %q want %q",
			resp.Header.Get("Sec-WebSocket-Accept"), want)
	}
	return nil
}

func sendMessage(fw *ws.FrameWriter, opcode int, payload []byte, fragSize int, withPing bool) error {
	if fragSize <= 0 || fragSize >= len(payload) {
		return fw.WriteMessage(opcode, payload)
	}
	for off := 0; off < len(payload); {
		end := off + fragSize
		if end > len(payload) {
			end = len(payload)
		}
		chunk := payload[off:end]
		fin := end == len(payload)
		if off == 0 {
			if err := fw.WriteFrame(fin, opcode, chunk); err != nil {
				return err
			}
		} else {
			if err := fw.WriteFrame(fin, ws.OpContinuation, chunk); err != nil {
				return err
			}
		}
		off = end
		if withPing && !fin && off == fragSize {
			// 控制帧允许穿插在分片消息中间（仅演示一次）。
			if err := fw.WritePing([]byte("are-you-there")); err != nil {
				return err
			}
		}
	}
	return nil
}

func pump(dec *ws.Decoder, br *bufio.Reader) error {
	for {
		buf := make([]byte, 32*1024)
		n, err := br.Read(buf)
		if n > 0 {
			events, derr := dec.Feed(buf[:n])
			for _, ev := range events {
				switch ev.Kind {
				case "message":
					kind := "text"
					if ev.OpCode == ws.OpBinary {
						kind = "binary"
					}
					if ev.OpCode == ws.OpText {
						fmt.Printf("RECV %s: %s\n", kind, string(ev.Data))
					} else {
						fmt.Printf("RECV %s: %x\n", kind, ev.Data)
					}
					return nil // 拿到 echo 即可返回；pong 只打印不返回
				case "ping":
					fmt.Printf("RECV ping: %x\n", ev.Data)
				case "pong":
					fmt.Printf("RECV pong: %q\n", string(ev.Data))
				case "close":
					fmt.Printf("RECV close: code=%d reason=%q\n", ws.CloseCode(ev.Data), ws.CloseReason(ev.Data))
					return io.EOF
				}
			}
			if derr != nil {
				return derr
			}
		}
		if err != nil {
			return err
		}
	}
}
