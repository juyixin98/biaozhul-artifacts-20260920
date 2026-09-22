package main

import (
	"context"
	"log"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"community-governance/internal/app"
	"community-governance/internal/config"
	"community-governance/internal/migrate"
)

func main() {
	cfg := config.Load()
	ctx := context.Background()

	pool, err := pgxpool.New(ctx, cfg.Database)
	if err != nil {
		log.Fatalf("connect pool: %v", err)
	}
	pool.Config().MaxConns = 10
	pool.Config().MaxConnLifetime = time.Hour

	// Migrations: run through a single connection.
	conn, err := pool.Acquire(ctx)
	if err != nil {
		log.Fatalf("acquire: %v", err)
	}
	if err := migrate.Migrate(ctx, conn.Conn()); err != nil {
		log.Fatalf("migrate: %v", err)
	}
	conn.Release()

	a := app.NewWithBootstrapToken(pool, cfg.PlatformBootstrapToken)
	srv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           a.Router(),
		ReadHeaderTimeout: 10 * time.Second,
	}
	log.Printf("listening on %s", cfg.HTTPAddr)
	if err := srv.ListenAndServe(); err != nil {
		log.Fatal(err)
	}
}
