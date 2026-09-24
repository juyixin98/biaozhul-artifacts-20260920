// Command udpsim serves the UDP reliable-transfer simulator over HTTP.
package main

import (
	"flag"
	"log"
	"net/http"
	"time"

	"udpreliable/internal/httpapi"
)

func main() {
	addr := flag.String("addr", ":8080", "HTTP listen address")
	flag.Parse()

	srv := &http.Server{
		Addr:              *addr,
		Handler:           httpapi.NewHandler(),
		ReadHeaderTimeout: 5 * time.Second,
	}
	log.Printf("udpsim listening on %s", *addr)
	log.Fatal(srv.ListenAndServe())
}
