// Command damsserver runs the DAMS audit backend HTTP API.
package main

import (
	"context"
	"log"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"dams.local/dams/internal/config"
	"dams.local/dams/internal/migrate"
	"dams.local/dams/internal/server"
)

func main() {
	cfg := config.Load()
	ctx := context.Background()

	if err := waitForPostgres(ctx, cfg.DatabaseURL, 30); err != nil {
		log.Fatalf("database unreachable: %v", err)
	}

	pool, err := pgxpool.New(ctx, cfg.DatabaseURL)
	if err != nil {
		log.Fatalf("connect pool: %v", err)
	}
	defer pool.Close()

	if cfg.MigrateOnStart {
		conn, err := pgx.Connect(ctx, cfg.DatabaseURL)
		if err != nil {
			log.Fatalf("connect for migration: %v", err)
		}
		if err := migrate.EnsureSchema(ctx, conn); err != nil {
			_ = conn.Close(ctx)
			log.Fatalf("migrate: %v", err)
		}
		_ = conn.Close(ctx)
		log.Print("migrations applied")
	}

	srv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           server.New(pool).Router(),
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       60 * time.Second,
		WriteTimeout:      120 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	log.Printf("DAMS listening on %s", cfg.HTTPAddr)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
}

func waitForPostgres(ctx context.Context, url string, attempts int) error {
	var lastErr error
	for i := 0; i < attempts; i++ {
		conn, err := pgx.Connect(ctx, url)
		if err == nil {
			_ = conn.Close(ctx)
			return nil
		}
		lastErr = err
		time.Sleep(time.Second)
	}
	return lastErr
}
