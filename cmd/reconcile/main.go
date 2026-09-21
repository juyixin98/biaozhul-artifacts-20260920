// Command reconcile runs the daily settlement + reconciliation job.
//
// Usage:
//
//	reconcile -admin-key sk_ad_... [-merchant <uuid>] [-date 2006-01-02]
//
// With no -merchant it processes every active merchant. The job is idempotent
// and crash-safe: re-running it never duplicates settlements or discrepancies.
package main

import (
	"context"
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/google/uuid"

	"github.com/clearsettle/clearsettle/internal/config"
	"github.com/clearsettle/clearsettle/internal/service"
	"github.com/clearsettle/clearsettle/internal/store"
)

func main() {
	adminKey := flag.String("admin-key", os.Getenv("CLEARSETTLE_ADMIN_KEY"), "platform admin API key")
	merchant := flag.String("merchant", "", "merchant id (default: all active merchants)")
	dateFlag := flag.String("date", "", "batch/run date YYYY-MM-DD (default: today UTC)")
	flag.Parse()

	if *adminKey == "" {
		log.Fatal("-admin-key or CLEARSETTLE_ADMIN_KEY is required")
	}
	day := time.Now().UTC()
	if *dateFlag != "" {
		d, err := time.Parse("2006-01-02", *dateFlag)
		if err != nil {
			log.Fatalf("invalid -date: %v", err)
		}
		day = d
	}

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	cfg := config.Load()
	pool, err := store.Connect(ctx, cfg.DatabaseURL)
	if err != nil {
		log.Fatalf("connect db: %v", err)
	}
	defer pool.Close()
	if err := store.Migrate(ctx, pool); err != nil {
		log.Fatalf("migrate: %v", err)
	}

	svc := service.New(pool, nil)
	actor, err := svc.Authenticate(ctx, *adminKey)
	if err != nil {
		log.Fatalf("authentication failed: %v", err)
	}
	if actor.Role != "admin" {
		log.Fatal("the supplied key is not an admin key")
	}

	merchants := []uuid.UUID{}
	if *merchant != "" {
		mid, err := uuid.Parse(*merchant)
		if err != nil {
			log.Fatalf("invalid merchant id: %v", err)
		}
		merchants = append(merchants, mid)
	} else {
		ids, err := svc.ListActiveMerchantIDs(ctx)
		if err != nil {
			log.Fatalf("list merchants: %v", err)
		}
		merchants = ids
	}

	for _, mid := range merchants {
		select {
		case <-ctx.Done():
			log.Print("interrupted; re-run the same command to resume")
			return
		default:
		}
		if _, err := svc.SettleDay(ctx, actor, mid, day); err != nil {
			log.Fatalf("settle merchant %s: %v", mid, err)
		}
		res, err := svc.Reconcile(ctx, actor, mid, day)
		if err != nil {
			log.Fatalf("reconcile merchant %s: %v", mid, err)
		}
		log.Printf("merchant=%s date=%s discrepancies=%d diff=%d cents",
			mid, day.Format("2006-01-02"),
			res.Run.DiscrepanciesCount, res.Run.DiffCents)
	}
}
