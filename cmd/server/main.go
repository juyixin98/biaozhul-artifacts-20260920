// Command server runs the CommunityVault content revision & moderation API.
package main

import (
	"context"
	"log"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"communityvault/internal/config"
	"communityvault/internal/content"
	httpserver "communityvault/internal/httpserver"
	"communityvault/internal/migrate"
	"communityvault/internal/moderation"
	"communityvault/internal/rules"
	"communityvault/internal/view"
)

func main() {
	ctx := context.Background()
	cfg := config.Load()

	// A single connection for migrations/seeding, before the pool starts.
	if cfg.RunMigrations {
		conn, err := pgx.Connect(ctx, cfg.DatabaseURL)
		if err != nil {
			log.Fatalf("connect for migrations: %v", err)
		}
		r := migrate.New(conn, cfg.MigrationsPath)
		if err := r.Migrate(ctx); err != nil {
			log.Fatalf("migrate: %v", err)
		}
		if cfg.RunSeed {
			if err := migrate.Seed(ctx, conn, cfg.SeedPath); err != nil {
				log.Fatalf("seed: %v", err)
			}
		}
		_ = conn.Close(ctx)
		log.Print("migrations and seed applied")
	}

	pool, err := pgxpool.New(ctx, cfg.DatabaseURL)
	if err != nil {
		log.Fatalf("pool: %v", err)
	}
	defer pool.Close()

	rs := rules.New(pool)
	cts := content.New(pool, rs)
	mod := moderation.New(pool, rs, cfg.ClaimTTL)
	vs := view.New(pool)

	srv := httpserver.NewServer(pool, cts, mod, rs, vs)
	httpSrv := &http.Server{
		Addr:              cfg.Addr,
		Handler:           srv.Router(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	stop := make(chan struct{})
	go recycleLoop(ctx, mod, stop)

	log.Printf("CommunityVault listening on %s (claim ttl %s)", cfg.Addr, cfg.ClaimTTL)
	if err := httpSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("server: %v", err)
	}
}

// recycleLoop periodically reopens tasks whose claims expired. Claiming also
// recycles inline, so this loop only covers the idle-queue case.
func recycleLoop(ctx context.Context, mod *moderation.Service, stop chan struct{}) {
	ticker := time.NewTicker(10 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-stop:
			return
		case <-ticker.C:
			if n, err := mod.Recycle(ctx); err != nil {
				log.Printf("recycle: %v", err)
			} else if n > 0 {
				log.Printf("recycled %d expired claim(s)", n)
			}
		}
	}
}
