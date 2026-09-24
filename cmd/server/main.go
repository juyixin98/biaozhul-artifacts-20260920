// Command registry-gc runs the local content-addressable image registry with
// safe layer-reference garbage collection.
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

	"layer-gc/internal/db"
	"layer-gc/internal/gc"
	"layer-gc/internal/httpapi"
	"layer-gc/internal/maintenance"
	"layer-gc/internal/registry"
	"layer-gc/internal/storage"
)

func main() {
	var (
		addr     = flag.String("addr", envOr("ADDR", "127.0.0.1:8080"), "listen address")
		dsn      = flag.String("dsn", envOr("DATABASE_URL", "postgres://gcuser:gcpass@127.0.0.1:5432/registry_gc?sslmode=disable"), "PostgreSQL DSN")
		dataDir  = flag.String("data-dir", envOr("DATA_DIR", "./data"), "on-disk blob store root")
		leaseTTL = flag.Duration("lease-ttl", 60*time.Second, "read lease TTL")
	)
	flag.Parse()

	logger := log.New(os.Stdout, "registry-gc ", log.LstdFlags|log.Lmsgprefix)

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	pgdb, err := db.Open(ctx, *dsn)
	if err != nil {
		logger.Fatalf("database: %v", err)
	}
	defer pgdb.Close()
	if err := db.Migrate(ctx, pgdb); err != nil {
		logger.Fatalf("migrate: %v", err)
	}
	logger.Printf("schema migrated")

	store, err := storage.New(*dataDir)
	if err != nil {
		logger.Fatalf("storage: %v", err)
	}
	reg := registry.New(pgdb, store)
	collector := gc.New(pgdb, store)
	cleaner := maintenance.NewCleaner(pgdb, store)

	srv := httpapi.NewServer(reg, collector, cleaner, logger)
	srv.LeaseTTL = *leaseTTL

	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           srv.Routes(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		logger.Printf("listening on %s (data=%s)", *addr, *dataDir)
		if err := httpSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logger.Fatalf("http: %v", err)
		}
	}()

	<-ctx.Done()
	logger.Printf("shutting down")
	shCtx, shCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shCancel()
	if err := httpSrv.Shutdown(shCtx); err != nil {
		logger.Printf("shutdown: %v", err)
	}
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
