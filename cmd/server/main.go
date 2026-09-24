// Command drf-scheduler starts the multi-resource DRF scheduling HTTP service.
package main

import (
	"flag"
	"log"
	"net/http"
	"os"

	"drf-scheduler/internal/api"
	"drf-scheduler/internal/scheduler"
)

func main() {
	defaultAddr := ":8080"
	if v := os.Getenv("ADDR"); v != "" {
		defaultAddr = v
	}
	addr := flag.String("addr", defaultAddr, "listen address (env ADDR overrides default)")
	flag.Parse()

	srv := api.NewServer(scheduler.New())
	log.Printf("DRF scheduler listening on %s", *addr)
	if err := http.ListenAndServe(*addr, srv.Mux()); err != nil {
		log.Fatal(err)
	}
}
