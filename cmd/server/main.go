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

	"targetcraft/internal/config"
	"targetcraft/internal/db"
	"targetcraft/internal/httpapi"
	"targetcraft/internal/service"
)

func main() {
	cfg := config.Load()

	gdb, err := db.Open(cfg.MySQLDSN)
	if err != nil {
		log.Fatalf("open db: %v", err)
	}
	if err := db.Migrate(gdb); err != nil {
		log.Fatalf("migrate: %v", err)
	}

	svc := service.New(gdb)
	svc.SetReservationTTL(cfg.ReservationTTL)

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	// Startup sweep recovers reservations orphaned by a previous process;
	// the ticker only accelerates cleanup afterwards.
	go svc.RunSweeper(ctx, cfg.SweepInterval)

	srv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           httpapi.NewRouter(svc),
		ReadHeaderTimeout: 5 * time.Second,
	}
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutdownCtx)
	}()

	log.Printf("targetcraft listening on %s", cfg.HTTPAddr)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("serve: %v", err)
	}
}
