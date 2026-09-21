package main

import (
	"context"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"targetcraft/internal/api"
	"targetcraft/internal/config"
	"targetcraft/internal/service"
	"targetcraft/internal/store"
)

func main() {
	cfg := config.FromEnv()

	db, err := store.Open(cfg.DatabaseDSN)
	if err != nil {
		log.Fatalf("connect database: %v", err)
	}

	if cfg.RunMigrations {
		if err := store.Migrate(db); err != nil {
			log.Fatalf("run migrations: %v", err)
		}
		log.Println("migrations applied")
	}

	if cfg.SeedSampleData {
		if err := store.Seed(db, time.Now().UTC()); err != nil {
			log.Fatalf("seed sample data: %v", err)
		}
		log.Println("sample data seeded")
	}

	svc := service.New(db, cfg.ReservationTTL)

	// Recovery on boot: release reservations left pending by a previous
	// process (crash or shutdown), then keep sweeping periodically. All
	// state is in MySQL — no in-memory timers are required for correctness.
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	go func() {
		if n, err := svc.SweepExpired(context.Background()); err != nil {
			log.Printf("startup sweep: %v", err)
		} else if n > 0 {
			log.Printf("startup sweep: released %d expired reservations", n)
		}
		ticker := time.NewTicker(cfg.SweepInterval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				if n, err := svc.SweepExpired(context.Background()); err != nil {
					log.Printf("sweep: %v", err)
				} else if n > 0 {
					log.Printf("sweep: released %d expired reservations", n)
				}
			}
		}
	}()

	router := api.NewRouter(svc, db)
	srv := &http.Server{Addr: ":" + cfg.Port, Handler: router}

	go func() {
		log.Printf("listening on :%s", cfg.Port)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("serve: %v", err)
		}
	}()

	<-ctx.Done()
	shutdownCtx, cancel := context.WithTimeout(context.Background(), cfg.ShutdownTimeout)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("shutdown: %v", err)
	}
	log.Println("stopped")
}
