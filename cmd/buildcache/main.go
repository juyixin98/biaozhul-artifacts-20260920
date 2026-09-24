// Command buildcache runs the local build-cache HTTP backend.
package main

import (
	"context"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"buildcache/internal/builder"
	"buildcache/internal/server"
	"buildcache/internal/store"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address")
	dataDir := flag.String("data", "./data", "data directory (sqlite db + blobs)")
	sourceRoot := flag.String("source-root", "./examples/workspace",
		"root directory for non-inline source paths")
	flag.Parse()

	if err := os.MkdirAll(*dataDir, 0o755); err != nil {
		log.Fatalf("create data dir: %v", err)
	}
	dbPath := filepath.Join(*dataDir, "cache.db")

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	st, err := store.Open(ctx, dbPath, *dataDir)
	if err != nil {
		log.Fatalf("open store: %v", err)
	}
	defer st.Close()

	if n, err := st.ResetStaleLeases(ctx); err != nil {
		log.Fatalf("recover stale leases: %v", err)
	} else if n > 0 {
		log.Printf("recovered %d interrupted build(s) from a previous process", n)
	}

	b, err := builder.New(st, *dataDir, *sourceRoot)
	if err != nil {
		log.Fatalf("builder init: %v", err)
	}

	srv := &http.Server{
		Addr:              *addr,
		Handler:           server.New(b).Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("buildcache listening on %s (data=%s source-root=%s)", *addr, *dataDir, *sourceRoot)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("listen: %v", err)
		}
	}()

	<-ctx.Done()
	log.Printf("shutting down")
	sctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = srv.Shutdown(sctx)
}
