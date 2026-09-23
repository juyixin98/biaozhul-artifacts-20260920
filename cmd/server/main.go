// Command twap-server runs the time-weighted average price HTTP service.
package main

import (
	"context"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"twap-service/internal/domain"
	"twap-service/internal/httpapi"
	"twap-service/internal/service"
	"twap-service/internal/store"
)

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func main() {
	addr := flag.String("addr", env("TWAP_ADDR", ":8080"), "listen address")
	dsn := flag.String("dsn",
		env("TWAP_DSN", "postgres://twap:twappw@127.0.0.1:5432/twap?sslmode=disable"),
		"PostgreSQL DSN")
	windowSec := flag.Int64("window-sec", int64FromEnv("TWAP_WINDOW_SEC", 60), "window length in seconds")
	lateSec := flag.Int64("late-tolerance-sec", int64FromEnv("TWAP_LATE_SEC", 300), "accepted late-data horizon")
	staleSec := flag.Int64("stale-horizon-sec", int64FromEnv("TWAP_STALE_SEC", 120), "carry-forward freshness horizon")
	futureSec := flag.Int64("future-grace-sec", int64FromEnv("TWAP_FUTURE_GRACE_SEC", 2), "future timestamp tolerance")
	conflictMode := flag.String("conflict-mode", env("TWAP_CONFLICT_MODE", "priority"),
		"source conflict mode: priority|reject")
	adminKey := flag.String("admin-key", env("TWAP_ADMIN_KEY", "dev-admin-key"),
		"HTTP Basic password for admin endpoints")
	flag.Parse()

	cfg := domain.Config{
		WindowSec:        *windowSec,
		LateToleranceSec: *lateSec,
		FutureGraceSec:   *futureSec,
		StaleHorizonSec:  *staleSec,
	}
	if err := cfg.Validate(); err != nil {
		log.Fatalf("invalid config: %v", err)
	}
	mode := service.ConflictMode(*conflictMode)
	if mode != service.ConflictPriority && mode != service.ConflictReject {
		log.Fatalf("invalid conflict mode %q", *conflictMode)
	}

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	// Startup (DB connect + schema migration) uses an independent context:
	// the signal context is only for triggering shutdown once running.
	st, err := store.New(context.Background(), *dsn)
	if err != nil {
		log.Fatalf("store: %v", err)
	}
	defer st.Close()

	svc := service.New(st, cfg, mode, time.Now)
	api := httpapi.New(svc, st, *adminKey, 5*time.Minute)

	srv := &http.Server{
		Addr:              *addr,
		Handler:           api.Router(),
		ReadHeaderTimeout: 10 * time.Second,
	}
	go func() {
		log.Printf("TWAP service listening on %s (window=%ds late=%ds stale=%ds conflict=%s)",
			*addr, cfg.WindowSec, cfg.LateToleranceSec, cfg.StaleHorizonSec, mode)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("listen: %v", err)
		}
	}()
	<-ctx.Done()
	log.Println("shutting down")
	shCtx, shCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shCancel()
	if err := srv.Shutdown(shCtx); err != nil {
		log.Printf("graceful shutdown failed: %v", err)
	}
}

func int64FromEnv(key string, def int64) int64 {
	if v := os.Getenv(key); v != "" {
		var n int64
		if _, err := time.ParseDuration(v + "s"); err == nil {
			// not used; kept to avoid accidental float env values
		}
		if parsed, err := parseInt64(v); err == nil {
			n = parsed
			return n
		}
	}
	return def
}

func parseInt64(s string) (int64, error) {
	var n int64
	for _, c := range s {
		if c < '0' || c > '9' {
			return 0, os.ErrInvalid
		}
		n = n*10 + int64(c-'0')
	}
	return n, nil
}
