// Command consistenthash runs the weighted consistent-hash routing and
// migration-plan HTTP service.
package main

import (
	"flag"
	"log"
	"net/http"

	"consistenthash/internal/server"
)

func main() {
	addr := flag.String("addr", ":8080", "listen address")
	flag.Parse()

	srv := server.New()
	log.Printf("consistent-hash service listening on %s", *addr)
	if err := http.ListenAndServe(*addr, srv.Handler()); err != nil {
		log.Fatalf("server error: %v", err)
	}
}
