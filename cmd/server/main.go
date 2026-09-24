// Command server runs the preemptible checkpoint scheduler simulation HTTP API.
package main

import (
	"flag"
	"log"
	"net/http"
	"time"

	"checkpoint-scheduler/api"
)

func main() {
	addr := flag.String("addr", ":8080", "listen address")
	flag.Parse()

	srv := &http.Server{
		Addr:              *addr,
		Handler:           api.NewHandler(),
		ReadHeaderTimeout: 5 * time.Second,
	}
	log.Printf("preemptible checkpoint scheduler listening on %s", *addr)
	if err := srv.ListenAndServe(); err != nil {
		log.Fatal(err)
	}
}
