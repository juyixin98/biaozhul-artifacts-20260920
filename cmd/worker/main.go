// Command worker runs settlement and reconciliation loops separately from the
// API. Run it alongside cmd/api (or instead of the in-process worker) for
// deployments that want background jobs isolated.
package main

import (
	"context"
	"log"
	"os/signal"
	"syscall"

	"github.com/clearsettle/clearsettle/internal/config"
	"github.com/clearsettle/clearsettle/internal/db"
	"github.com/clearsettle/clearsettle/internal/recon"
	"github.com/clearsettle/clearsettle/internal/settle"
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

	if _, err := db.Migrate(ctx, pool); err != nil {
		log.Fatalf("migrate: %v", err)
	}

	w := worker.New(settle.New(pool, cfg.WorkerLockID), recon.New(pool), cfg.WorkerLockID, cfg.SettleHorizon)
	log.Printf("worker %s starting (settle horizon %s)", cfg.WorkerLockID, cfg.SettleHorizon)
	w.Run(ctx)
}
