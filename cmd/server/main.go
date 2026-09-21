// SIRCC — Security Incident Response Coordination Center backend.
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

	"sircc/internal/httpapi"
	"sircc/internal/incident"
	"sircc/internal/migrations"
	"sircc/internal/reminder"
)

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func main() {
	var (
		databaseURL = env("DATABASE_URL", "postgres://sircc:postgres@localhost:5432/sircc?sslmode=disable")
		addr        = env("ADDR", ":8080")
		interval    = 5 * time.Second
	)
	if v := os.Getenv("REMINDER_INTERVAL"); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			interval = d
		}
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	if err := migrations.Up(databaseURL); err != nil {
		log.Fatalf("migrations: %v", err)
	}
	pool, err := pgxpool.New(ctx, databaseURL)
	if err != nil {
		log.Fatalf("db pool: %v", err)
	}
	defer pool.Close()

	worker := &reminder.Worker{Pool: pool}
	go worker.Start(ctx, interval)

	srv := &http.Server{
		Addr:              addr,
		Handler:           httpapi.NewRouter(incident.NewService(pool)),
		ReadHeaderTimeout: 10 * time.Second,
	}
	go func() {
		<-ctx.Done()
		shutCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutCtx)
	}()

	log.Printf("sircc listening on %s", addr)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("http server: %v", err)
	}
}
