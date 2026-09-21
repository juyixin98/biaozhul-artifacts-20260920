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

	"github.com/clearsettle/clearsettle/internal/admin"
	"github.com/clearsettle/clearsettle/internal/api"
	"github.com/clearsettle/clearsettle/internal/auth"
	"github.com/clearsettle/clearsettle/internal/config"
	"github.com/clearsettle/clearsettle/internal/db"
	"github.com/clearsettle/clearsettle/internal/payments"
	"github.com/clearsettle/clearsettle/internal/recon"
	"github.com/clearsettle/clearsettle/internal/settle"
	"github.com/clearsettle/clearsettle/internal/store"
	"github.com/clearsettle/clearsettle/internal/worker"
)

func main() {
	cfg := config.Get()
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	pool, err := db.Connect(ctx, cfg.DatabaseURL)
	if err != nil {
		log.Fatalf("connect db: %v", err)
	}
	defer pool.Close()

	n, err := db.Migrate(ctx, pool)
	if err != nil {
		log.Fatalf("migrate: %v", err)
	}
	if n > 0 {
		log.Printf("applied %d migration(s)", n)
	}

	if err := bootstrapAdmin(ctx, pool, cfg); err != nil {
		log.Fatalf("bootstrap admin: %v", err)
	}

	svcAdmin := admin.New(pool)
	svcPayments := payments.New(pool)
	svcSettle := settle.New(pool, cfg.WorkerLockID)
	svcRecon := recon.New(pool)

	router := api.NewRouter(api.Deps{
		Pool:          pool,
		Q:             store.New(pool),
		JWTSecret:     cfg.JWTSecret,
		SettleHorizon: cfg.SettleHorizon,
		Admin:         svcAdmin,
		Payments:      svcPayments,
		Settle:        svcSettle,
		Recon:         svcRecon,
	})

	srv := &http.Server{Addr: cfg.HTTPAddr, Handler: router, ReadHeaderTimeout: 10 * time.Second}

	// Background settlement + reconciliation loop runs in-process by default;
	// set WORKER_ENABLED=false to run the worker separately (cmd/worker).
	if os.Getenv("WORKER_ENABLED") != "false" {
		w := worker.New(svcSettle, svcRecon, cfg.WorkerLockID, cfg.SettleHorizon)
		go w.Run(ctx)
	}

	go func() {
		log.Printf("ClearSettle listening on %s", cfg.HTTPAddr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http: %v", err)
		}
	}()

	<-ctx.Done()
	log.Println("shutdown requested")
	shCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	_ = srv.Shutdown(shCtx)
}

// bootstrapAdmin creates the initial admin from env on first boot. The password
// is never logged; rotate it after first login.
func bootstrapAdmin(ctx context.Context, pool *pgxpool.Pool, cfg config.Config) error {
	q := store.New(pool)
	_, err := q.GetUserByEmail(ctx, cfg.Bootstrap.AdminEmail)
	if err == nil {
		return nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return err
	}
	hash, err := auth.HashPassword(cfg.Bootstrap.AdminPassword)
	if err != nil {
		return err
	}
	if _, err := q.CreateUser(ctx, store.CreateUserParams{
		Email: cfg.Bootstrap.AdminEmail, PasswordHash: hash, Role: auth.RoleAdmin,
	}); err != nil {
		return err
	}
	log.Printf("bootstrapped admin user %s", cfg.Bootstrap.AdminEmail)
	return nil
}
