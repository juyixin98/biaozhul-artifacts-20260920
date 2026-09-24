// Command deadlockd runs the task resource deadlock-check service.
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

	"deadlockcheck/internal/api"
	"deadlockcheck/internal/config"
	"deadlockcheck/internal/core"
	"deadlockcheck/internal/store"
)

func main() {
	if err := run(); err != nil {
		log.Fatalf("deadlockd: %v", err)
	}
}

func run() error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	st, err := store.Open(ctx, cfg.DatabaseURL)
	if err != nil {
		return err
	}
	defer st.Pool.Close()

	if err := st.Migrate(ctx); err != nil {
		return err
	}
	ver, err := st.SchemaVersion(ctx)
	if err != nil {
		return err
	}
	log.Printf("schema migrated at version %s", ver)

	signer, err := st.BootstrapSigner(ctx, cfg.EvidenceSecret)
	if err != nil {
		return err
	}

	svc := core.NewService(st.Pool, signer, cfg.AgingPerSec)

	// Restart recovery: rebuild the wait-for graph from persistent holds and
	// pending requests, and report the task states found on disk.
	stats, err := svc.Recover(ctx)
	if err != nil {
		return err
	}
	log.Printf("restart recovery complete: active tasks %v", stats)

	go svc.RunSweeper(ctx, cfg.SweepInterval, func(ids []int64) {
		log.Printf("timeout sweep: tasks moved to uncertain: %v (resources retained)", ids)
	})

	srv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           api.NewServer(svc).Router(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	serverErr := make(chan error, 1)
	go func() {
		log.Printf("deadlockd listening on %s", cfg.HTTPAddr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			serverErr <- err
		}
		close(serverErr)
	}()

	select {
	case <-ctx.Done():
		log.Printf("shutdown signal received")
	case err := <-serverErr:
		if err != nil {
			return err
		}
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		return err
	}
	log.Printf("server stopped cleanly")
	_ = os.Stdout.Sync()
	return nil
}
