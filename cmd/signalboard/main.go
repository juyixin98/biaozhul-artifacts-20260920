// Command signalboard runs the SignalBoard API server.
package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	_ "time/tzdata" // embed IANA tz database so the static binary needs no OS files

	"github.com/jackc/pgx/v5/pgxpool"

	"signalboard/internal/db"
	"signalboard/internal/migrate"
	"signalboard/internal/server"
)

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func main() {
	addr := env("ADDR", ":8080")
	dbURL := env("DATABASE_URL", "postgres://signalboard:signalboard@localhost:5432/signalboard?sslmode=disable")
	seedDemo := env("SEED_DEMO", "") == "true"
	log := slog.New(slog.NewTextHandler(os.Stdout, nil))

	ctx := context.Background()
	pool, err := pgxpool.New(ctx, dbURL)
	if err != nil {
		log.Error("parse DATABASE_URL", "err", err)
		os.Exit(1)
	}
	defer pool.Close()

	// Wait for the database (compose startup ordering).
	deadline := time.Now().Add(60 * time.Second)
	for {
		if err = pool.Ping(ctx); err == nil {
			break
		}
		if time.Now().After(deadline) {
			log.Error("database not reachable", "err", err)
			os.Exit(1)
		}
		time.Sleep(time.Second)
	}

	if err := migrate.Up(ctx, pool); err != nil {
		log.Error("migrations failed", "err", err)
		os.Exit(1)
	}
	q := db.New(pool)
	if seedDemo {
		if err := server.SeedDemo(ctx, pool, q, os.Stdout); err != nil {
			log.Error("seed failed", "err", err)
			os.Exit(1)
		}
	}

	srv := &http.Server{
		Addr:              addr,
		Handler:           server.New(q, pool),
		ReadHeaderTimeout: 10 * time.Second,
	}
	go func() {
		log.Info("listening", "addr", addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("serve", "err", err)
			os.Exit(1)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = srv.Shutdown(shutdownCtx)
}
