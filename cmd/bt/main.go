// Command bt runs the behavior-tree resumable-execution HTTP backend.
//
// Configuration via environment:
//
//	BT_DATABASE_URL  PostgreSQL connection URL (required)
//	BT_HTTP_ADDR     listen address (default ":8080")
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

	"bt/internal/api"
	"bt/internal/engine"
	"bt/internal/store"
	"bt/internal/stub"
)

func main() {
	dbURL := os.Getenv("BT_DATABASE_URL")
	if dbURL == "" {
		log.Fatal("BT_DATABASE_URL is required")
	}
	addr := os.Getenv("BT_HTTP_ADDR")
	if addr == "" {
		addr = ":8080"
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	poolCfg, err := pgxpool.ParseConfig(dbURL)
	if err != nil {
		log.Fatalf("parse database url: %v", err)
	}
	poolCfg.MaxConns = 20
	pool, err := pgxpool.NewWithConfig(ctx, poolCfg)
	if err != nil {
		log.Fatalf("connect database: %v", err)
	}
	defer pool.Close()

	if err := pool.Ping(ctx); err != nil {
		log.Fatalf("ping database: %v", err)
	}

	st := store.New(pool)
	if err := st.Migrate(ctx); err != nil {
		log.Fatalf("migrate: %v", err)
	}

	eng := engine.New(st, stub.NewRegistry(), engine.Options{})
	if err := eng.ReapOrphans(ctx); err != nil {
		log.Fatalf("recover orphans: %v", err)
	}

	srv := &http.Server{
		Addr:              addr,
		Handler:           api.NewServer(eng),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("behavior tree backend listening on %s", addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http server: %v", err)
		}
	}()

	<-ctx.Done()
	log.Printf("shutdown signal received")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("graceful shutdown failed: %v", err)
	}
}
