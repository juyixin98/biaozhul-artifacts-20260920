// 命令 wsserver 提供一个纯 net/http 的 WebSocket echo 服务，
// 用于演示 ws 包里的本地帧状态机。
package main

import (
	"errors"
	"flag"
	"io"
	"log"
	"net"
	"net/http"
	"time"

	"wsreassemble/ws"
)

func main() {
	addr := flag.String("addr", ":8080", "监听地址")
	maxFrame := flag.Int64("maxframe", ws.DefaultMaxFrameSize, "单帧载荷上限（字节）")
	maxMsg := flag.Int64("maxmsg", ws.DefaultMaxMessageSize, "重组消息上限（字节）")
	idle := flag.Duration("idle", 120*time.Second, "空闲读超时；0 表示不限制")
	flag.Parse()

	upgrader := ws.Upgrader{MaxFrameSize: *maxFrame, MaxMessageSize: *maxMsg}

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok\n"))
	})
	mux.HandleFunc("/ws", func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r)
		if err != nil {
			log.Printf("upgrade failed from %s: %v", r.RemoteAddr, err)
			return
		}
		log.Printf("connection upgraded: %s", r.RemoteAddr)
		serveEcho(conn, *idle)
	})

	log.Printf("WebSocket echo server listening on %s (ws://host/ws)", *addr)
	if err := http.ListenAndServe(*addr, mux); err != nil {
		log.Fatal(err)
	}
}

func serveEcho(conn *ws.Conn, idle time.Duration) {
	defer conn.Close()
	resetDeadline := func() {
		if idle > 0 {
			_ = conn.RawConn().SetReadDeadline(time.Now().Add(idle))
		}
	}
	resetDeadline()

	for {
		events, err := conn.ReadEvents()
		for _, ev := range events {
			switch ev.Kind {
			case "message":
				log.Printf("recv message opcode=%d len=%d -> echo", ev.OpCode, len(ev.Data))
				if werr := conn.WriteMessage(ev.OpCode, ev.Data); werr != nil {
					log.Printf("write echo: %v", werr)
					return
				}
			case "ping":
				_ = conn.WritePong(ev.Data) // RFC 6455 §5.5.3：Pong 载荷原样回显
			case "pong":
				// 主动心跳之外收到的 Pong 可以忽略。
			case "close":
				code := ws.CloseCode(ev.Data)
				log.Printf("recv close code=%d reason=%q", code, ws.CloseReason(ev.Data))
				echoCode := code
				if echoCode == 0 {
					echoCode = ws.CloseNormalClosure
				}
				_ = conn.SendClose(echoCode, "")
				return
			}
		}

		if err != nil {
			var fe *ws.FrameError
			switch {
			case errors.As(err, &fe):
				log.Printf("protocol error code=%d: %v -> closing", fe.Code, fe.Msg)
				_ = conn.SendClose(fe.Code, fe.Msg)
			case errors.Is(err, io.EOF), errors.Is(err, net.ErrClosed):
				log.Printf("peer disconnected")
			default:
				log.Printf("read: %v", err)
			}
			return
		}
		resetDeadline()
	}
}
