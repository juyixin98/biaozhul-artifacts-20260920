// Command ximbox runs the cross-chain message inbox HTTP server.
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

	"github.com/jackc/pgx/v5/pgxpool"

	"ximbox/internal/config"
	"ximbox/internal/cryptoenvelope"
	"ximbox/internal/inbox"
	"ximbox/internal/server"
	"ximbox/internal/store"
)

func main() {
	cfg, err := config.Load()
	if err != nil {
		log.Fatalf("config: %v", err)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	pool, err := pgxpool.New(ctx, cfg.DatabaseURL)
	if err != nil {
		log.Fatalf("connect db: %v", err)
	}
	defer pool.Close()

	pingCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	if err := pool.Ping(pingCtx); err != nil {
		log.Fatalf("ping db: %v", err)
	}

	st := store.New(pool)
	if err := st.Migrate(ctx); err != nil {
		log.Fatalf("migrate: %v", err)
	}
	if err := st.EnsureChains(ctx, []string{cryptoenvelope.ChainA, cryptoenvelope.ChainB}); err != nil {
		log.Fatalf("ensure chains: %v", err)
	}

	svc := inbox.New(st, cryptoenvelope.TrustedPublicKeys())
	if hook := inbox.NewEnvCrashHook(); hook != nil {
		svc.SetCrashHook(hook)
	}
	// Crash recovery: complete any prefix execution interrupted by a crash.
	if err := svc.RecoverOnStartup(ctx); err != nil {
		log.Printf("startup recovery error: %v", err)
	}

	srv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           server.New(svc, st),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("ximbox listening on %s", cfg.HTTPAddr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http: %v", err)
		}
	}()

	<-ctx.Done()
	log.Printf("shutting down")
	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shutdownCancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("graceful shutdown failed: %v", err)
		os.Exit(1)
	}
}
