// Command costlens-server runs the CostLens HTTP API.
package main

import (
	"context"
	"log"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"

	"costlens/internal/api"
	"costlens/internal/config"
	"costlens/internal/migrate"
	"costlens/internal/service"
)

func main() {
	cfg, err := config.Load()
	if err != nil {
		log.Fatalf("config: %v", err)
	}

	ctx := context.Background()
	if err := withConn(ctx, cfg.DatabaseURL, func(conn *pgx.Conn) error {
		return migrate.Migrate(ctx, conn)
	}); err != nil {
		log.Fatalf("migrate: %v", err)
	}
	log.Print("migrations applied")

	pool, err := pgxpoolNew(ctx, cfg.DatabaseURL)
	if err != nil {
		log.Fatalf("pool: %v", err)
	}
	defer pool.Close()

	if cfg.SeedOnStart {
		if err := seedDemo(ctx, pool); err != nil {
			log.Fatalf("seed: %v", err)
		}
		log.Print("demo data seeded")
	}

	srv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           api.NewRouter(service.New(pool)),
		ReadHeaderTimeout: 10 * time.Second,
	}
	log.Printf("CostLens listening on %s", cfg.HTTPAddr)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("server: %v", err)
	}
}
