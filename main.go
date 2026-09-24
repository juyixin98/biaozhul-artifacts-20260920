// Command vcreg serves a local multi-replica simulation of a multi-version
// register coordinated with vector clocks. It uses only the Go standard
// library (net/http, encoding/json, flag, log).
package main

import (
	"flag"
	"log"
	"net/http"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:18080", "listen address")
	flag.Parse()

	srv := NewServer(NewStore())
	log.Printf("vcreg listening on http://%s (vector-clock multi-version register)", *addr)
	log.Printf("try: curl -s http://%s/health", *addr)
	if err := http.ListenAndServe(*addr, srv); err != nil {
		log.Fatal(err)
	}
}
