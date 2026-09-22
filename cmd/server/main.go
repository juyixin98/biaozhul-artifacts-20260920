// Command geoterritory starts the GeoTerritory backend: runs migrations,
// seeds demo organizations, serves the HTTP API and (optionally) runs the
// reassignment worker.
package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"geoterritory/internal/api"
	"geoterritory/internal/config"
	"geoterritory/internal/store"
	"geoterritory/internal/worker"
)

func main() {
	cfg := config.Load()

	db, err := store.Open(cfg.MySQLDSN)
	if err != nil {
		log.Fatalf("connect mysql: %v", err)
	}
	if err := store.Migrate(db, cfg.MigrationDir); err != nil {
		log.Fatalf("migrate: %v", err)
	}
	if err := store.SeedAPIKeys(db, cfg.SeedAPIKeys); err != nil {
		log.Fatalf("seed organizations: %v", err)
	}
	log.Printf("migrations applied; serving on %s", cfg.HTTPAddr)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	if cfg.WorkerEnabled {
		w := worker.New(db, cfg.WorkerTick, cfg.BatchSize, cfg.StaleJobAfter, cfg.LockTimeoutS)
		go w.Run(ctx)
		log.Printf("reassignment worker enabled (tick=%s batch=%d stale=%s)",
			cfg.WorkerTick, cfg.BatchSize, cfg.StaleJobAfter)
	}

	srv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           api.NewServer(db, cfg).Router(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http server: %v", err)
		}
	}()

	<-ctx.Done()
	log.Printf("shutdown signal received")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("graceful shutdown: %v", err)
		os.Exit(1)
	}
	log.Printf("stopped cleanly")
}
