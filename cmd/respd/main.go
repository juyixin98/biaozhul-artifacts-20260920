// Command respd starts the RESP pipeline HTTP server.
//
// Usage:
//
//	respd -addr :8080
//
// It speaks RESP2 over HTTP POST only (see internal/server).
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

	"respd/internal/kv"
	"respd/internal/server"
)

func main() {
	addr := flag.String("addr", envOr("RESPD_ADDR", ":8080"), "listen address (or RESPD_ADDR)")
	maxBody := flag.Int64("max-body", server.DefaultMaxBodyBytes, "maximum request body in bytes")
	flag.Parse()

	srv := &http.Server{
		Addr:              *addr,
		Handler:           server.New(server.Config{Store: kv.NewStore(), MaxBodyBytes: *maxBody}),
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	go func() {
		log.Printf("respd listening on %s (POST /resp, GET /healthz, GET /commands)", *addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("server error: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop
	log.Printf("shutting down")

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Printf("graceful shutdown failed: %v", err)
	}
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
