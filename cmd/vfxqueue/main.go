// Command vfxqueue runs the VFX render queue backend.
//
// Usage:
//
//	vfxqueue serve             # HTTP API + 2 local render workers (default)
//	vfxqueue api               # HTTP API only
//	vfxqueue worker            # render workers only
//	vfxqueue migrate           # apply database migrations and exit
//
// Configuration is via environment variables (see internal/config/config.go
// and README.md).
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

	"vfxqueue/internal/apiserver"
	"vfxqueue/internal/config"
	"vfxqueue/internal/db/gen"
	"vfxqueue/internal/migrate"
	"vfxqueue/internal/reconcile"
	"vfxqueue/internal/seed"
	"vfxqueue/internal/storage"
	"vfxqueue/internal/worker"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	cmd := os.Args[1]

	cfg, err := config.Load()
	if err != nil {
		log.Fatalf("config: %v", err)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	switch cmd {
	case "migrate":
		conn, err := pgx.Connect(ctx, cfg.DatabaseURL)
		if err != nil {
			log.Fatalf("connect: %v", err)
		}
		defer conn.Close(ctx)
		if err := migrate.Run(ctx, conn); err != nil {
			log.Fatalf("migrate: %v", err)
		}
		log.Printf("migrations applied")
	case "api", "worker", "serve":
		if err := run(ctx, cmd, cfg); err != nil {
			log.Fatalf("%s: %v", cmd, err)
		}
	default:
		usage()
	}
}

func run(ctx context.Context, mode string, cfg config.Config) error {
	pool, err := pgxpool.New(ctx, cfg.DatabaseURL)
	if err != nil {
		return err
	}
	defer pool.Close()

	if err := waitForDB(ctx, pool); err != nil {
		return err
	}
	conn, err := pool.Acquire(ctx)
	if err != nil {
		return err
	}
	if err := migrate.Run(ctx, conn.Conn()); err != nil {
		conn.Release()
		return err
	}
	conn.Release()

	if err := storage.EnsureDir(cfg.AssetsDir); err != nil {
		return err
	}
	if err := storage.EnsureDir(cfg.OutputsDir); err != nil {
		return err
	}

	q := gen.New(pool)

	// Demo data is convenient for docker compose; skip with SEED=0.
	if os.Getenv("SEED") != "0" && mode != "worker" {
		if err := seed.Ensure(ctx, pool, q, cfg.AssetsDir); err != nil {
			return err
		}
	}

	if mode != "api" {
		// Repair crashes before any worker claims work.
		rec := reconcile.New(pool, q, cfg.OutputsDir)
		if err := rec.Run(ctx); err != nil {
			return err
		}
	}

	errCh := make(chan error, 2)

	var httpSrv *http.Server
	if mode != "worker" {
		srv := apiserver.New(pool, q, cfg)
		httpSrv = &http.Server{
			Addr:              cfg.HTTPAddr,
			Handler:           srv.Router(),
			ReadHeaderTimeout: 10 * time.Second,
		}
		go func() {
			log.Printf("HTTP API listening on %s", cfg.HTTPAddr)
			if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
				errCh <- err
			}
		}()
	}

	var w *worker.Worker
	workerDone := make(chan struct{})
	if mode != "api" {
		w = worker.New(pool, q, cfg)
		go func() {
			w.Run(ctx)
			close(workerDone)
		}()
	} else {
		close(workerDone)
	}

	select {
	case <-ctx.Done():
		log.Printf("shutdown signal received")
	case err := <-errCh:
		return err
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if httpSrv != nil {
		if err := httpSrv.Shutdown(shutdownCtx); err != nil {
			log.Printf("http shutdown: %v", err)
		}
	}
	<-workerDone
	log.Printf("stopped")
	return nil
}

func waitForDB(ctx context.Context, pool *pgxpool.Pool) error {
	deadline := time.Now().Add(30 * time.Second)
	var lastErr error
	for time.Now().Before(deadline) {
		if err := pool.Ping(ctx); err == nil {
			return nil
		} else {
			lastErr = err
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(500 * time.Millisecond):
		}
	}
	return lastErr
}

func usage() {
	log.Fatalf("usage: vfxqueue [serve|api|worker|migrate]")
}
