// Command ws-server serves a WebSocket echo endpoint implemented with the
// local ws package (Go standard library only).
package main

import (
	"flag"
	"fmt"
	"log"
	"net/http"
	"strconv"
	"strings"

	"wsreassemble/ws"
)

// parseSize accepts plain byte counts ("4096") or binary suffixes
// ("1K"/"1KiB", "2M"/"2MiB", "1GiB").
func parseSize(s string) (int, error) {
	var mult int64 = 1
	switch {
	case strings.HasSuffix(s, "KiB"):
		s, mult = s[:len(s)-3], 1<<10
	case strings.HasSuffix(s, "MiB"):
		s, mult = s[:len(s)-3], 1<<20
	case strings.HasSuffix(s, "GiB"):
		s, mult = s[:len(s)-3], 1<<30
	case strings.HasSuffix(s, "K"), strings.HasSuffix(s, "k"):
		s, mult = s[:len(s)-1], 1<<10
	case strings.HasSuffix(s, "M"), strings.HasSuffix(s, "m"):
		s, mult = s[:len(s)-1], 1<<20
	case strings.HasSuffix(s, "G"), strings.HasSuffix(s, "g"):
		s, mult = s[:len(s)-1], 1<<30
	}
	v, err := strconv.ParseInt(s, 10, 64)
	if err != nil || v < 0 {
		return 0, fmt.Errorf("invalid size %q", s)
	}
	return int(v * mult), nil
}

func main() {
	addr := flag.String("addr", ":8080", "listen address")
	maxFrameFlag := flag.String("max-frame", "1MiB", "maximum single frame payload (e.g. 65536, 1MiB)")
	maxMsgFlag := flag.String("max-message", "4MiB", "maximum reassembled message (e.g. 1048576, 4MiB)")
	flag.Parse()

	maxFrame, err := parseSize(*maxFrameFlag)
	if err != nil {
		log.Fatalf("bad --max-frame %q: %v", *maxFrameFlag, err)
	}
	maxMsg, err := parseSize(*maxMsgFlag)
	if err != nil {
		log.Fatalf("bad --max-message %q: %v", *maxMsgFlag, err)
	}

	logger := log.New(log.Writer(), "ws-server ", log.LstdFlags|log.Lmicroseconds)
	opts := &ws.Options{MaxFrame: maxFrame, MaxMessage: maxMsg, Logger: logger}

	mux := http.NewServeMux()
	mux.HandleFunc("/ws", func(w http.ResponseWriter, r *http.Request) {
		ws.Serve(w, r, opts)
	})
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	})
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		_, _ = w.Write([]byte("WebSocket message reassembly demo\n" +
			"  GET /ws     - WebSocket echo endpoint (RFC 6455)\n" +
			"  GET /health - health check\n\n" +
			"Every complete text or binary message is echoed back verbatim.\n" +
			"Frames must be masked; control frames may interleave fragments.\n"))
	})

	logger.Printf("listening on %s (max frame=%d bytes, max message=%d bytes)", *addr, maxFrame, maxMsg)
	if err := http.ListenAndServe(*addr, mux); err != nil {
		log.Fatal(err)
	}
}
