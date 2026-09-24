package main

import (
	"context"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"
)

func main() {
	addr := flag.String("addr", envOr("JSONRPC_ADDR", ":8080"),
		"listen address (env JSONRPC_ADDR)")
	concurrency := flag.Int("concurrency", envIntOr("JSONRPC_CONCURRENCY", 64),
		"max concurrent method handlers, 0 = unlimited (env JSONRPC_CONCURRENCY)")
	maxBody := flag.Int64("max-body", envInt64Or("JSONRPC_MAX_BODY", 10<<20),
		"max request body in bytes (env JSONRPC_MAX_BODY)")
	flag.Parse()

	gw := NewGateway(Config{Concurrency: *concurrency, MaxBodyBytes: *maxBody})
	gw.registerBuiltins()

	srv := &http.Server{
		Addr:              *addr,
		Handler:           withLogging(gw),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("jsonrpc-gateway listening on %s (concurrency=%d)", *addr, *concurrency)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("server error: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop
	log.Print("shutting down…")

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Printf("graceful shutdown failed: %v", err)
	}
}

// withLogging logs one line per HTTP call. Notification-only payloads return
// 204, which is visible here.
func withLogging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rw := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rw, r)
		log.Printf("%s %s -> %d", r.Method, r.URL.Path, rw.status)
	})
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (s *statusRecorder) WriteHeader(code int) {
	s.status = code
	s.ResponseWriter.WriteHeader(code)
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

func envInt64Or(key string, def int64) int64 {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil {
			return n
		}
	}
	return def
}
