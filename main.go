// Command respd serves a RESP2 key/value pipeline over HTTP.
//
// Usage:
//
//	go run . [-addr :7379] [-max-body 134217728] [-idle-ttl 5m]
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
	addr := flag.String("addr", envOr("RESPD_ADDR", ":7379"),
		"listen address (env RESPD_ADDR)")
	maxBody := flag.Int64("max-body", 128*1024*1024,
		"maximum accepted HTTP request body in bytes")
	idleTTL := flag.Duration("idle-ttl", server.DefaultIdleTTL,
		"idle lifetime of named sessions")
	flag.Parse()

	store := kv.New()
	srv := server.New(store,
		server.WithMaxBody(*maxBody),
		server.WithIdleTTL(*idleTTL),
	)
	defer srv.Close()

	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           srv.Mux(),
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(),
		syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	go func() {
		log.Printf("respd listening on %s (POST /resp raw RESP2, POST /exec JSON)", *addr)
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("listen: %v", err)
		}
	}()

	<-ctx.Done()
	log.Printf("shutdown signal received, draining connections…")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := httpSrv.Shutdown(shutdownCtx); err != nil {
		log.Printf("graceful shutdown failed: %v", err)
	}
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
