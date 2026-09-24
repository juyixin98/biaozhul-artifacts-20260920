// Command placementd is a topology-aware GPU task placement service.
//
// It is a pure backend HTTP service (no UI): given a cluster topology
// (device memory, NUMA affinity, interconnect costs) and a task
// (replica count, per-replica memory), it assigns replicas to devices so
// that all hard constraints hold and group-internal communication cost is
// minimized. It never talks to real GPUs.
package main

import (
	"context"
	"errors"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"topology-aware-gpu-scheduler/internal/api"
)

func main() {
	addr := flag.String("addr", envOr("PORT", ":8080"), "listen address (env PORT overrides the default)")
	flag.Parse()

	logger := log.New(os.Stdout, "placementd ", log.LstdFlags|log.Lmicroseconds)
	srv := &http.Server{
		Addr:              *addr,
		Handler:           api.NewLoggedMux(logger),
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	go func() {
		logger.Printf("topology-aware GPU placement service listening on %s", *addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("server error: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop
	logger.Printf("shutdown signal received")

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Fatalf("graceful shutdown failed: %v", err)
	}
	logger.Printf("stopped")
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return ":" + v
	}
	return def
}
