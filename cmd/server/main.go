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

	"github.com/clearsettle/clearsettle/internal/api"
	"github.com/clearsettle/clearsettle/internal/config"
	"github.com/clearsettle/clearsettle/internal/domain"
	"github.com/clearsettle/clearsettle/internal/service"
	"github.com/clearsettle/clearsettle/internal/store"
)

func main() {
	cfg := config.Load()
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	pool, err := store.Connect(ctx, cfg.DatabaseURL)
	if err != nil {
		log.Fatalf("connect db: %v", err)
	}
	defer pool.Close()

	if err := store.Migrate(ctx, pool); err != nil {
		log.Fatalf("migrate: %v", err)
	}
	log.Print("migrations applied")

	svc := service.New(pool, domain.RealClock{})
	if cfg.SeedDemo {
		if err := seedDemo(ctx, svc); err != nil {
			log.Fatalf("seed demo data: %v", err)
		}
	}

	srv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           api.NewServer(svc),
		ReadHeaderTimeout: 10 * time.Second,
	}
	go func() {
		log.Printf("ClearSettle listening on %s", cfg.HTTPAddr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http: %v", err)
		}
	}()

	<-ctx.Done()
	log.Print("shutting down")
	shutdownCtx, c := context.WithTimeout(context.Background(), 10*time.Second)
	defer c()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("graceful shutdown: %v", err)
	}
}

// seedDemo creates one platform admin key (printed once) and a demo merchant
// with an operator key. It is a no-op once data exists.
func seedDemo(ctx context.Context, svc *service.Service) error {
	// Bootstrap admin: use a fixed key only when no admin keys exist.
	const bootstrapAdminKey = "sk_ad_demoadminkey000000000000000000"
	adminActor, err := svc.Authenticate(ctx, bootstrapAdminKey)
	if err != nil {
		// First boot: mint the bootstrap admin directly.
		if _, err := svc.BootstrapAdmin(ctx, bootstrapAdminKey); err != nil {
			return err
		}
		adminActor, err = svc.Authenticate(ctx, bootstrapAdminKey)
		if err != nil {
			return err
		}
		log.Printf("bootstrapped platform admin key: %s", bootstrapAdminKey)
	}

	merchants, err := svc.ListMerchants(ctx, adminActor)
	if err != nil {
		return err
	}
	if len(merchants) > 0 {
		return nil
	}
	out, err := svc.CreateMerchant(ctx, adminActor, service.CreateMerchantInput{
		Name: "Demo Coffee Co.",
	})
	if err != nil {
		return err
	}
	log.Printf("seeded demo merchant %s", out.Merchant.ID)
	log.Printf("demo operator key (save it): %s", out.OperatorKey)
	_ = os.Stdout.Sync()
	return nil
}
