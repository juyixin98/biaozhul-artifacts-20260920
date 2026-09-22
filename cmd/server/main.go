// Command server runs migrations, bootstraps users, reconciles crashed
// renders, starts the HTTP API and the (at most) two render workers.
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

	"github.com/vfxqueue/renderq/internal/api"
	"github.com/vfxqueue/renderq/internal/config"
	"github.com/vfxqueue/renderq/internal/db/dbgen"
	"github.com/vfxqueue/renderq/internal/storage"
	"github.com/vfxqueue/renderq/internal/worker"
	"github.com/vfxqueue/renderq/migrations"
)

func main() {
	logger := log.New(os.Stderr, "[renderq] ", log.LstdFlags|log.Lmicroseconds)
	cfg, err := config.Load()
	if err != nil {
		logger.Fatalf("config: %v", err)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// A single connection for migrations keeps startup simple.
	conn, err := pgx.Connect(ctx, cfg.DatabaseURL)
	if err != nil {
		logger.Fatalf("connect db: %v", err)
	}
	if err := migrations.Migrate(ctx, conn); err != nil {
		logger.Fatalf("migrate: %v", err)
	}
	_ = conn.Close(ctx)

	pool, err := pgxpool.New(ctx, cfg.DatabaseURL)
	if err != nil {
		logger.Fatalf("pool: %v", err)
	}
	defer pool.Close()
	if err := pool.Ping(ctx); err != nil {
		logger.Fatalf("ping db: %v", err)
	}

	if err := bootstrapUsers(ctx, pool, cfg); err != nil {
		logger.Fatalf("bootstrap users: %v", err)
	}

	store, err := storage.New(cfg.DataDir)
	if err != nil {
		logger.Fatalf("storage: %v", err)
	}

	// Recover BEFORE starting workers so no frame is claimed twice across
	// a restart and partial output is reconciled.
	if err := worker.Recover(ctx, pool, store, logger); err != nil {
		logger.Fatalf("crash recovery: %v", err)
	}

	srv := api.NewServer(pool, store)
	httpServer := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           srv.Router(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	wk := worker.New(pool, store, cfg.LeaseSeconds, cfg.RenewSeconds, cfg.PollMillis, nil, logger)
	workerCtx, cancelWorkers := context.WithCancel(context.Background())
	go wk.Run(workerCtx, cfg.Workers)
	logger.Printf("started %d worker(s), lease=%ds renew=%ds", cfg.Workers, cfg.LeaseSeconds, cfg.RenewSeconds)

	go func() {
		logger.Printf("http listening on %s", cfg.HTTPAddr)
		if err := httpServer.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Fatalf("http: %v", err)
		}
	}()

	<-ctx.Done()
	logger.Printf("shutting down")
	cancelWorkers()

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := httpServer.Shutdown(shutdownCtx); err != nil {
		logger.Printf("http shutdown: %v", err)
	}
}

// bootstrapUsers creates the configured dev users if they do not exist.
// Existing users are left untouched (including key rotation).
func bootstrapUsers(ctx context.Context, pool *pgxpool.Pool, cfg config.Config) error {
	q := dbgen.New(pool)
	for _, b := range cfg.BootstrapUsers {
		_, err := q.GetUserByName(ctx, b.Username)
		if err == nil {
			continue
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		hash := api.HashAPIKey(b.APIKey)
		if _, err := q.CreateUser(ctx, dbgen.CreateUserParams{
			Username: b.Username, Role: b.Role, ApiKeyHash: hash,
		}); err != nil {
			return err
		}
		log.Printf("bootstrapped %s user %q", b.Role, b.Username)
	}
	return nil
}
