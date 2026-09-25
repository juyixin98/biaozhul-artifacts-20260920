// Command pim-server serves the priority-inheritance simulator over HTTP.
//
// Usage:
//
//	pim-server -addr :8080
package main

import (
	"flag"
	"log"
	"net/http"

	"pim/internal/httpapi"
)

func main() {
	addr := flag.String("addr", ":8080", "listen address")
	flag.Parse()

	srv := httpapi.NewServer()
	log.Printf("pim-server listening on %s (GET /healthz, POST /api/simulate)", *addr)
	if err := http.ListenAndServe(*addr, srv.Handler()); err != nil {
		log.Fatal(err)
	}
}
