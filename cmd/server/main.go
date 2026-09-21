// Command server runs the CommunityVault content revision & moderation API.
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

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"communityvault/internal/config"
	db "communityvault/internal/db"
	httpapi "communityvault/internal/httpapi"
	"communityvault/internal/migrate"
)

func main() {
	cfg := config.Load()
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// A single connection is enough to apply migrations before the pool starts.
	migConn, err := pgx.Connect(ctx, cfg.DatabaseURL)
	if err != nil {
		log.Fatalf("connect for migrations: %v", err)
	}
	if err := migrate.Up(ctx, migConn); err != nil {
		log.Fatalf("migrate: %v", err)
	}
	_ = migConn.Close(ctx)

	pool, err := pgxpool.New(ctx, cfg.DatabaseURL)
	if err != nil {
		log.Fatalf("pool: %v", err)
	}
	defer pool.Close()
	if err := pool.Ping(ctx); err != nil {
		log.Fatalf("ping: %v", err)
	}

	// Periodic safety net for timed-out claims. The claim path also requeues
	// opportunistically; this loop guarantees stuck claims do not linger.
	go reaper(ctx, pool, cfg.ClaimTTL)

	srv := &http.Server{
		Addr:              cfg.Addr,
		Handler:           httpapi.New(pool, cfg.ClaimTTL.Seconds()),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("CommunityVault listening on %s (claim ttl %s)", cfg.Addr, cfg.ClaimTTL)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("listen: %v", err)
		}
	}()

	<-ctx.Done()
	log.Println("shutting down")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("shutdown: %v", err)
	}
}

func reaper(ctx context.Context, pool *pgxpool.Pool, ttl time.Duration) {
	// Run at half the claim TTL (min 5s) so expiry is noticed promptly.
	interval := ttl / 2
	if interval < 5*time.Second {
		interval = 5 * time.Second
	}
	t := time.NewTicker(interval)
	defer t.Stop()
	q := db.New(pool)
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			rows, err := q.RequeueExpiredClaims(ctx)
			if err != nil {
				log.Printf("reaper: %v", err)
				continue
			}
			if len(rows) > 0 {
				log.Printf("reaper: requeued %d expired task(s)", len(rows))
			}
		}
	}
}

func init() {
	// Simple structured logging through the standard logger.
	log.SetFlags(log.LstdFlags | log.Lmsgprefix)
	log.SetOutput(os.Stdout)
}
