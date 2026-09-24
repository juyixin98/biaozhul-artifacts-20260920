// Command taskq starts the persistent task-cancellation/race HTTP server.
//
// Usage:
//
//	go run ./cmd/taskq [-addr :8080] [-data ./data] [-lease 30s] [-sweep 1s]
//
// State lives in -data (snapshot.json + wal.log). Remove the directory for a
// clean start; restarting keeps all acknowledged state.
package main

import (
	"context"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"taskq/internal/api"
	"taskq/internal/store"
)

func main() {
	addr := flag.String("addr", envOr("TASKQ_ADDR", ":8080"), "listen address")
	dataDir := flag.String("data", envOr("TASKQ_DATA", "./data"), "directory for snapshot and WAL")
	leaseTTL := flag.Duration("lease", durationEnv("TASKQ_LEASE", 30*time.Second), "worker lease TTL")
	sweep := flag.Duration("sweep", durationEnv("TASKQ_SWEEP", 1*time.Second), "background lease-expiry sweep interval")
	flag.Parse()

	logger := log.New(os.Stdout, "taskq ", log.LstdFlags|log.Lmicroseconds)

	st, err := store.Open(*dataDir, store.Options{
		LeaseTTL:      *leaseTTL,
		SweepInterval: *sweep,
	})
	if err != nil {
		logger.Fatalf("open store in %s: %v", *dataDir, err)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	st.StartSweeper(ctx, *sweep)

	srv := &http.Server{
		Addr:              *addr,
		Handler:           (&api.Server{Store: st, Logger: logger}).NewHandler(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		logger.Printf("listening on %s (data=%s lease=%s sweep=%s)", *addr, *dataDir, *leaseTTL, *sweep)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logger.Fatalf("http server: %v", err)
		}
	}()

	<-ctx.Done()
	logger.Printf("shutdown signal received, draining...")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		logger.Printf("graceful shutdown: %v", err)
	}
	if err := st.Close(); err != nil {
		logger.Printf("close store: %v", err)
	}
	logger.Printf("stopped")
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func durationEnv(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return def
}
