// Command deadlock-server runs the task resource deadlock check service.
package main

import (
	"context"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"deadlockcheck/internal/httpapi"
	"deadlockcheck/internal/service"
	"deadlockcheck/internal/store"
)

func getenv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func main() {
	dsn := getenv("DATABASE_DSN",
		"postgres://deadlock:deadlock_pw_068@localhost:5432/deadlock_db?sslmode=disable")
	addr := getenv("HTTP_ADDR", ":8068")
	sweepInterval, _ := time.ParseDuration(getenv("SWEEP_INTERVAL", "1s"))
	cfg := service.DefaultConfig()
	if ms, _ := strconv.Atoi(os.Getenv("AGING_STEP_MS")); ms > 0 {
		cfg.AgingStep = time.Duration(ms) * time.Millisecond
	}
	if v, _ := strconv.Atoi(os.Getenv("AGING_CAP")); v > 0 {
		cfg.AgingCap = v
	}

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	st, err := store.Open(ctx, dsn)
	if err != nil {
		log.Fatalf("store: %v", err)
	}
	defer st.Close()
	log.Printf("connected and migrated")

	svc, err := service.New(ctx, st, cfg)
	if err != nil {
		log.Fatalf("service: %v", err)
	}

	// Restart recovery: bump fence epoch, move running holders to uncertain.
	rep, err := svc.Recover(ctx)
	if err != nil {
		log.Fatalf("recovery: %v", err)
	}
	log.Printf("recovery: epoch=%d running->uncertain=%v waiting=%v",
		rep.FenceEpoch, rep.ToUncertain, rep.StillWaiting)

	// Background lease sweeper.
	sw := newSweeper(svc, sweepInterval)
	sw.Start(ctx)
	defer sw.Stop()

	srv := &http.Server{
		Addr:              addr,
		Handler:           httpapi.New(svc),
		ReadHeaderTimeout: 5 * time.Second,
	}
	go func() {
		log.Printf("listening on %s", addr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("http: %v", err)
		}
	}()

	<-ctx.Done()
	log.Printf("shutting down")
	shCtx, shCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer shCancel()
	_ = srv.Shutdown(shCtx)
}
