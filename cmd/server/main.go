// Command server runs the localhost-only circuit-breaker demo and acceptance
// harness. It never contacts a production system: the downstream is an
// in-process fake and the clock is virtual.
package main

import (
	"flag"
	"log"
	"net/http"
	"time"

	"breakerhalfopen/internal/breaker"
	"breakerhalfopen/internal/httpapi"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address (localhost only)")
	flag.DurationVar(&coolDown, "cooldown", 10*time.Second, "OPEN cool-down before half-open probes")
	flag.IntVar(&windowSize, "window", 5, "sliding sample window size")
	flag.IntVar(&failThreshold, "fail-threshold", 3, "failures in window that trip the breaker")
	flag.IntVar(&maxProbes, "max-probes", 2, "max concurrent half-open probes")
	flag.IntVar(&probeSuccess, "probe-success", 2, "consecutive probe successes needed to close")
	flag.Parse()

	cfg := breaker.Config{
		WindowSize:               windowSize,
		FailureThreshold:         failThreshold,
		OpenCoolDown:             coolDown,
		MaxProbeCalls:            maxProbes,
		HalfOpenSuccessThreshold: probeSuccess,
	}

	svc := httpapi.NewService(cfg)
	log.Printf("circuit breaker demo listening on http://%s (cooldown=%s window=%d fail=%d probes=%d/%d)",
		*addr, coolDown, windowSize, failThreshold, probeSuccess, maxProbes)

	srv := &http.Server{
		Addr:              *addr,
		Handler:           svc.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("server error: %v", err)
	}
}

// Flags bound at package scope for flag.DurationVar convenience.
var (
	coolDown      time.Duration
	windowSize    int
	failThreshold int
	maxProbes     int
	probeSuccess  int
)
