// Command stub runs the deterministic, scenario-driven metrics stub.
package main

import (
	"flag"
	"log"
	"net/http"
	"time"

	"github.com/example/rollout/internal/metricstub"
)

func main() {
	addr := flag.String("addr", ":8081", "listen address")
	flag.Parse()

	srv := &http.Server{
		Addr:              *addr,
		Handler:           metricstub.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}
	log.Printf("metrics stub listening on %s (scenarios: healthy, spike, degraded, insufficient, blackout, late)", *addr)
	if err := srv.ListenAndServe(); err != nil {
		log.Fatal(err)
	}
}
