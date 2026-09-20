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

	"sircc/internal/config"
	dblib "sircc/internal/db"
	"sircc/internal/httpapi"
	"sircc/internal/service"
)

func main() {
	cfg := config.Load()
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	pool, err := connectPool(ctx, cfg.DatabaseURL)
	if err != nil {
		log.Fatalf("connect database: %v", err)
	}
	defer pool.Close()

	if err := runMigrations(ctx, cfg, pool); err != nil {
		log.Fatalf("migrate: %v", err)
	}

	svc := service.New(pool, cfg)
	srv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           httpapi.Router(svc, svc.Q()),
		ReadHeaderTimeout: 10 * time.Second,
	}

	// Persistent reminder loop; catches up on anything due while down.
	scheduler := service.NewScheduler(svc, cfg.ReminderInterval, cfg.ReminderBatchSize)
	go scheduler.Run(ctx)

	go func() {
		log.Printf("SIRCC API listening on %s", cfg.HTTPAddr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http server: %v", err)
		}
	}()

	<-ctx.Done()
	log.Printf("shutting down")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("graceful shutdown: %v", err)
	}
}

func connectPool(ctx context.Context, url string) (*pgxpool.Pool, error) {
	cfg, err := pgxpool.ParseConfig(url)
	if err != nil {
		return nil, err
	}
	cfg.MaxConns = 10
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, err
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, err
	}
	return pool, nil
}

func runMigrations(ctx context.Context, cfg config.Config, pool *pgxpool.Pool) error {
	conn, err := pool.Acquire(ctx)
	if err != nil {
		return err
	}
	defer conn.Release()
	if _, err := conn.Exec(ctx, `CREATE SCHEMA IF NOT EXISTS sircc`); err != nil {
		// schema is optional; default public search_path is fine.
		log.Printf("create schema: %v (continuing)", err)
	}
	if err := dblib.Migrate(ctx, conn.Conn(), cfg.MigrationsDir); err != nil {
		// Retry briefly to tolerate docker-compose startup ordering.
		for attempt := 1; attempt <= 5 && ctx.Err() == nil; attempt++ {
			log.Printf("migration attempt %d/5: %v", attempt, err)
			time.Sleep(time.Duration(attempt) * time.Second)
			if err = dblib.Migrate(ctx, conn.Conn(), cfg.MigrationsDir); err == nil {
				break
			}
		}
	}
	return err
}

func init() {
	// Ensure timestamps render in UTC across the service.
	if os.Getenv("TZ") == "" {
		_ = os.Setenv("TZ", "UTC")
	}
}
