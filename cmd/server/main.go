// Command dagscheduler runs the local HTTP API for the DAG scheduler.
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

	"dagscheduler/scheduler"
	"dagscheduler/server"
)

func main() {
	addr := flag.String("addr", envOr("DAGSCHEDULER_ADDR", ":8080"),
		"listen address (env DAGSCHEDULER_ADDR)")
	flag.Parse()

	logger := log.New(os.Stdout, "dagscheduler: ", log.LstdFlags|log.Lmsgprefix)

	registry := scheduler.NewDefaultRegistry()
	engine := scheduler.New(registry, scheduler.WithSink(logSink(logger)))
	srv := &http.Server{
		Addr:              *addr,
		Handler:           server.New(engine).Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	go func() {
		logger.Printf("listening on %s", *addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Fatalf("http server: %v", err)
		}
	}()

	<-ctx.Done()
	logger.Printf("shutdown signal received")

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		logger.Printf("http shutdown: %v", err)
	}
	engine.Close()
	logger.Printf("stopped")
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// logSink prints every state-change event as structured JSON.
func logSink(logger *log.Logger) scheduler.Sink {
	return func(ev scheduler.Event) {
		logger.Printf("event seq=%d run=%s type=%s node=%s status=%s attempt=%d/%d err=%q reason=%q",
			ev.Seq, ev.RunID, ev.Type, ev.NodeID, ev.Status,
			ev.Attempt, ev.MaxAttempts, ev.Error, ev.Reason)
	}
}
