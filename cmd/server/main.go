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

	"github.com/example/artifact-promotion/internal/api"
	"github.com/example/artifact-promotion/internal/blob"
	"github.com/example/artifact-promotion/internal/config"
	"github.com/example/artifact-promotion/internal/core"
	"github.com/example/artifact-promotion/internal/migrate"
	"github.com/jackc/pgx/v5/pgxpool"
)

func main() {
	cfg := config.Load()
	ctx := context.Background()

	pool, err := pgxpool.New(ctx, cfg.DatabaseURL)
	if err != nil {
		log.Fatalf("connect postgres: %v", err)
	}
	defer pool.Close()
	if err := pool.Ping(ctx); err != nil {
		log.Fatalf("ping postgres: %v", err)
	}
	if err := migrate.Apply(ctx, pool); err != nil {
		log.Fatalf("apply migrations: %v", err)
	}

	blobs, err := blob.New(cfg.BlobRoot)
	if err != nil {
		log.Fatalf("open blob store: %v", err)
	}
	svc := core.NewService(pool, blobs, core.Hooks{})

	// Any attempt left 'running' was interrupted before its pointer commit
	// completed; the old version is still live. Mark them accordingly.
	n, err := svc.ReconcileInterrupted(ctx, 0)
	if err != nil {
		log.Fatalf("reconcile interrupted attempts: %v", err)
	}
	if n > 0 {
		log.Printf("reconciled %d interrupted attempt(s) from before restart", n)
	}

	srv := &http.Server{
		Addr:              cfg.ListenAddr,
		Handler:           api.NewRouter(svc),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		sig := make(chan os.Signal, 1)
		signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
		<-sig
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutdownCtx)
	}()

	log.Printf("listening on %s (blob root %s)", cfg.ListenAddr, cfg.BlobRoot)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("serve: %v", err)
	}
}
