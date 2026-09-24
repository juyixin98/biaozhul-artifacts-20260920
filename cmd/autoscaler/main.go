// Command autoscaler runs the offline hysteresis autoscaler as an HTTP server.
package main

import (
	"flag"
	"log"
	"net/http"

	"autoscaler/internal/api"
	"autoscaler/internal/scaler"
)

func main() {
	addr := flag.String("addr", ":8080", "listen address")
	replicas := flag.Int("replicas", 2, "initial replica count (clamped to [min_replicas, max_replicas])")
	flag.Parse()

	s := scaler.New(scaler.DefaultConfig(), *replicas)
	h := api.NewServer(s).Handler()

	log.Printf("autoscaler listening on %s, initial replicas=%d", *addr, *replicas)
	log.Printf("endpoints: GET /healthz | GET|PUT /v1/config | POST /v1/metrics | POST /v1/evaluate | GET /v1/state | GET /v1/decisions | POST /v1/reset")
	log.Fatal(http.ListenAndServe(*addr, h))
}
