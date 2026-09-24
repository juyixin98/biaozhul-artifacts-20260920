// Command sensorhealth runs the sensor health-determination HTTP server.
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

	"sensorhealth/internal/api"
	"sensorhealth/internal/config"
	"sensorhealth/internal/crypto"
	"sensorhealth/internal/domain"
	"sensorhealth/internal/service"
	"sensorhealth/internal/store"
)

func main() {
	addr := flag.String("addr", envOr("ADDR", ":8080"), "listen address")
	dsn := flag.String("db", envOr("DB_PATH", "sensorhealth.db"), "SQLite database path (use file:... for DSN options)")
	hmacSecret := flag.String("hmac-secret", envOr("HMAC_SECRET", "dev-shared-secret"), "shared HMAC secret for ingestion auth")
	adminToken := flag.String("admin-token", envOr("ADMIN_TOKEN", "dev-admin-token"), "bearer token for admin endpoints")
	sweepInterval := flag.Duration("sweep-interval", envDur("SWEEP_INTERVAL", time.Second), "background stale sweep interval")
	seed := flag.Bool("seed", os.Getenv("SEED") == "1", "seed example device types + devices on startup")
	flag.Parse()

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	st, err := store.Open(ctx, *dsn)
	if err != nil {
		log.Fatalf("open store: %v", err)
	}
	defer st.Close()

	svc := service.New(st, time.Now)
	svc.StartStaleSweeper(ctx, *sweepInterval)

	if *seed {
		if err := seedDemo(ctx, svc); err != nil {
			log.Fatalf("seed: %v", err)
		}
	}

	srv := &http.Server{
		Addr:              *addr,
		Handler:           api.NewServer(svc, crypto.NewSigner([]byte(*hmacSecret)), *adminToken).Routes(),
		ReadHeaderTimeout: 10 * time.Second,
	}
	go func() {
		log.Printf("sensor health server listening on %s (db=%s)", *addr, *dsn)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("serve: %v", err)
		}
	}()

	<-ctx.Done()
	log.Println("shutting down")
	shutdownCtx, c := context.WithTimeout(context.Background(), 5*time.Second)
	defer c()
	_ = srv.Shutdown(shutdownCtx)
}

func seedDemo(ctx context.Context, svc *service.Service) error {
	// A noisy environmental sensor (quick frozen threshold) and a stationary
	// contact sensor (lenient frozen threshold, proving not every stationary
	// device is flagged).
	for _, dt := range []string{"temp", "contact"} {
		if _, err := svc.GetConfig(ctx, dt); err == nil {
			continue
		}
		c := config.DefaultRuleConfig(dt, 0)
		if dt == "contact" {
			c.FrozenEnterCount = 100
			c.FrozenEnterMinDuration = domain.Duration(10 * time.Minute)
		}
		if _, err := svc.UpdateConfig(ctx, c); err != nil {
			return err
		}
	}
	if _, _, err := svc.RegisterDevice(ctx, "temp-1", "temp"); err != nil {
		return err
	}
	if _, _, err := svc.RegisterDevice(ctx, "contact-1", "contact"); err != nil {
		return err
	}
	log.Println("seeded device types {temp, contact} and devices {temp-1, contact-1}")
	return nil
}

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func envDur(k string, d time.Duration) time.Duration {
	if v := os.Getenv(k); v != "" {
		if parsed, err := time.ParseDuration(v); err == nil {
			return parsed
		}
	}
	return d
}
