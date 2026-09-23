// Command httpframe serves the offline HTTP/1.1 framing parser API.
// It never opens outbound connections: every byte is parsed locally.
package main

import (
	"flag"
	"log"
	"net/http"

	"httpframe/internal/server"
)

func main() {
	addr := flag.String("addr", ":8080", "listen address")
	maxBody := flag.Int64("max-body", 10<<20, "maximum accepted entity body in bytes (<=0 unlimited)")
	maxInput := flag.Int64("max-input", 16<<20, "maximum accepted wrapper request body in bytes")
	flag.Parse()

	cfg := server.Config{MaxBody: *maxBody, MaxInputBytes: *maxInput}
	log.Printf("httpframe listening on %s (max-body=%d, max-input=%d)", *addr, *maxBody, *maxInput)
	if err := http.ListenAndServe(*addr, server.New(cfg)); err != nil {
		log.Fatal(err)
	}
}
