package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os/signal"
	"syscall"
	"time"

	_ "time/tzdata" // embed IANA tz database so the static binary works without /usr/share/zoneinfo

	"signalboard/internal/api"
	"signalboard/internal/config"
	"signalboard/internal/pgdb"
)

func main() {
	cfg := config.Load()
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	pool, err := pgdb.Connect(ctx, cfg.DatabaseURL)
	if err != nil {
		log.Fatalf("database: %v", err)
	}
	defer pool.Close()

	applied, err := pgdb.Migrate(ctx, pool)
	if err != nil {
		log.Fatalf("migrate: %v", err)
	}
	if len(applied) > 0 {
		log.Printf("applied migrations: %v", applied)
	} else {
		log.Printf("database schema up to date")
	}

	srv := &http.Server{
		Addr:              cfg.Addr,
		Handler:           api.NewServer(pool, cfg).Router(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("SignalBoard listening on %s", cfg.Addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("listen: %v", err)
		}
	}()

	<-ctx.Done()
	log.Printf("shutdown signal received")

	shutdownCtx, cancel := context.WithTimeout(context.Background(), cfg.ShutdownTimeout)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("graceful shutdown failed: %v", err)
	}
}
