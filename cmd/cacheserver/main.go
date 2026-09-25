// Command cacheserver runs the HTTP model-artifact cache.
//
// Usage:
//
//	cacheserver -addr :8080 -cache-dir ./cache -max-object-size 100M
package main

import (
	"context"
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"

	"modelcache/cacheapi"
	"modelcache/store"
)

func main() {
	addr := flag.String("addr", envOr("CACHE_ADDR", ":8080"), "listen address")
	cacheDir := flag.String("cache-dir", envOr("CACHE_DIR", "./cache"), "cache root directory (separate from work dirs)")
	maxSize := flag.Int64("max-object-size", 100<<20, "maximum object size in bytes (default 100 MiB)")
	flag.Parse()

	logger := log.New(os.Stderr, "cacheserver ", log.LstdFlags|log.Lmicroseconds)

	st, err := store.New(store.Options{Root: *cacheDir, MaxObjectSize: *maxSize})
	if err != nil {
		logger.Fatalf("store init: %v", err)
	}
	if n, err := st.Sweep(context.Background()); err != nil {
		logger.Printf("warning: startup sweep failed: %v", err)
	} else if n > 0 {
		logger.Printf("swept %d orphaned temp upload(s) from previous runs", n)
	}

	srv := cacheapi.NewServer(st, logger)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	if err := cacheapi.ListenAndServe(ctx, *addr, srv.Handler(), logger); err != nil {
		logger.Fatalf("server error: %v", err)
	}
	logger.Printf("shutdown complete")
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
