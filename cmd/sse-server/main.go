// Command sse-server runs the persistent SSE event service.
package main

import (
	"context"
	"errors"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"sseserver/internal/broker"
	"sseserver/internal/server"
	"sseserver/internal/store"
)

func main() {
	addr := flag.String("addr", envOr("SSE_ADDR", ":8080"), "listen address (env SSE_ADDR)")
	dataDir := flag.String("data-dir", envOr("SSE_DATA_DIR", "./data"), "directory for the WAL file (env SSE_DATA_DIR)")
	maxEvents := flag.Int("retention-events", envIntOr("SSE_RETENTION_EVENTS", 10000), "max retained events; 0 = unlimited (env SSE_RETENTION_EVENTS)")
	maxAge := flag.Duration("retention-age", envDurationOr("SSE_RETENTION_AGE", 24*time.Hour), "max event age; 0 = no age limit (env SSE_RETENTION_AGE)")
	heartbeat := flag.Duration("heartbeat", envDurationOr("SSE_HEARTBEAT", 15*time.Second), "heartbeat interval (env SSE_HEARTBEAT)")
	bufferSize := flag.Int("sub-buffer", envIntOr("SSE_SUB_BUFFER", 64), "per-subscriber live-event buffer before slow-consumer disconnect (env SSE_SUB_BUFFER)")
	fsync := flag.Bool("fsync", envBoolOr("SSE_FSYNC", false), "fsync the WAL after every append (env SSE_FSYNC)")
	flag.Parse()

	logger := log.New(os.Stdout, "sse-server ", log.LstdFlags|log.Lmicroseconds)

	st, err := store.Open(store.Options{
		Dir:       *dataDir,
		MaxEvents: *maxEvents,
		MaxAge:    *maxAge,
		Sync:      *fsync,
	})
	if err != nil {
		logger.Fatalf("open store: %v", err)
	}
	defer func() { _ = st.Close() }()

	b := broker.New(st, broker.Config{BufferSize: *bufferSize}, logger)
	srv := server.New(b, st, server.Config{
		Heartbeat:  *heartbeat,
		MaxDataLen: 1 << 20,
	}, logger)

	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	go func() {
		logger.Printf("listening on %s (data=%s, retention events=%d age=%s, heartbeat=%s, buffer=%d, fsync=%t)",
			*addr, *dataDir, *maxEvents, *maxAge, *heartbeat, *bufferSize, *fsync)
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Fatalf("http server: %v", err)
		}
	}()

	<-ctx.Done()
	logger.Printf("shutting down...")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := httpSrv.Shutdown(shutdownCtx); err != nil {
		logger.Printf("shutdown: %v", err)
	}
	logger.Printf("stopped")
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envIntOr(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func envBoolOr(key string, def bool) bool {
	if v := os.Getenv(key); v != "" {
		switch v {
		case "1", "true", "TRUE", "yes":
			return true
		case "0", "false", "FALSE", "no":
			return false
		}
	}
	return def
}

func envDurationOr(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return def
}
