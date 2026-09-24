// Command server is the progressive-rollout decision engine.
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

	"github.com/example/rollout/internal/api"
	"github.com/example/rollout/internal/config"
	"github.com/example/rollout/internal/eval"
	"github.com/example/rollout/internal/metrics"
	"github.com/example/rollout/internal/store"
)

func main() {
	cfg, err := config.Load()
	if err != nil {
		log.Fatalf("config: %v", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	pg, err := store.NewPG(ctx, cfg.DatabaseURL)
	if err != nil {
		log.Fatalf("connect postgres: %v", err)
	}
	defer pg.Close()

	if err := pg.Migrate(ctx); err != nil {
		log.Fatalf("migrate: %v", err)
	}
	if err := pg.Ping(ctx); err != nil {
		log.Fatalf("postgres ping: %v", err)
	}
	log.Printf("postgres ready and migrated")

	fetcher := metrics.NewClient(cfg.StubURL)
	evaluator := eval.New(fetcher)
	rel := eval.NewReleaser(pg, evaluator)

	srv := api.NewServer(cfg, pg, rel)
	httpSrv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           srv.Router,
		ReadHeaderTimeout: 10 * time.Second,
	}

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	go func() {
		log.Printf("rollout decision server listening on %s (stub=%s)", cfg.HTTPAddr, cfg.StubURL)
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http: %v", err)
		}
	}()

	<-stop
	log.Printf("shutting down")
	shCtx, shCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer shCancel()
	if err := httpSrv.Shutdown(shCtx); err != nil {
		log.Printf("graceful shutdown failed: %v", err)
	}
}
