// Command sensorhealth-server runs the sensor health determination service.
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

	"sensorhealth/internal/clock"
	"sensorhealth/internal/cryptox"
	"sensorhealth/internal/engine"
	"sensorhealth/internal/httpapi"
	"sensorhealth/internal/store"
	"sensorhealth/internal/webhook"
)

func main() {
	addr := flag.String("addr", envOr("SENSOR_ADDR", ":8080"), "HTTP listen address")
	dbPath := flag.String("db", envOr("SENSOR_DB", "sensorhealth.db"), "SQLite database path")
	clockMode := flag.String("clock", envOr("SENSOR_CLOCK", "virtual"), "clock mode: real|virtual")
	ingestSecret := flag.String("ingest-secret", os.Getenv("SENSOR_INGEST_SECRET"), "HMAC secret for signed ingestion")
	adminToken := flag.String("admin-token", os.Getenv("SENSOR_ADMIN_TOKEN"), "bearer token for admin endpoints")
	webhookURL := flag.String("webhook-url", os.Getenv("SENSOR_WEBHOOK_URL"), "alert webhook URL (empty disables)")
	webhookSecret := flag.String("webhook-secret", os.Getenv("SENSOR_WEBHOOK_SECRET"), "HMAC secret for webhook signing")
	sweepInterval := flag.Duration("sweep-interval", 500*time.Millisecond, "stale sweep interval")
	flag.Parse()

	if *ingestSecret == "" {
		s, err := cryptox.NewSecret()
		if err != nil {
			log.Fatalf("generate ingest secret: %v", err)
		}
		*ingestSecret = s
		log.Printf("SENSOR_INGEST_SECRET not set; generated ephemeral secret: %s", *ingestSecret)
	}
	if *adminToken == "" {
		s, err := cryptox.NewSecret()
		if err != nil {
			log.Fatalf("generate admin token: %v", err)
		}
		*adminToken = s
		log.Printf("SENSOR_ADMIN_TOKEN not set; generated ephemeral token: %s", *adminToken)
	}
	if *webhookURL != "" && *webhookSecret == "" {
		log.Fatalf("--webhook-url set but --webhook-secret is empty; refusing to send unsigned alerts")
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	st, err := store.Open(ctx, *dbPath)
	if err != nil {
		log.Fatalf("open store: %v", err)
	}
	defer st.Close()

	var clk clock.Clock
	var vclk *clock.Virtual
	switch *clockMode {
	case "virtual":
		vclk = clock.NewVirtual(time.Now().UTC())
		clk = vclk
	case "real":
		clk = clock.Real{}
	default:
		log.Fatalf("unknown clock mode %q (want real|virtual)", *clockMode)
	}

	eng, err := engine.New(ctx, st, clk)
	if err != nil {
		log.Fatalf("engine init: %v", err)
	}
	if *webhookURL != "" {
		sink := webhook.NewSink(*webhookURL, *webhookSecret, st, clk)
		eng.SetSink(sink)
		log.Printf("webhook delivery enabled -> %s", *webhookURL)
	}

	h := httpapi.NewHandler(eng, st, clk, vclk, httpapi.Config{
		IngestSecret: *ingestSecret,
		AdminToken:   *adminToken,
		ReplayWindow: 5 * time.Minute,
	})
	srv := &http.Server{Addr: *addr, Handler: h, ReadHeaderTimeout: 5 * time.Second}

	go func() {
		t := time.NewTicker(*sweepInterval)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				if _, err := eng.Sweep(context.Background()); err != nil {
					log.Printf("sweep error: %v", err)
				}
			}
		}
	}()

	go func() {
		log.Printf("sensor-health service listening on %s (clock=%s, db=%s)", *addr, *clockMode, *dbPath)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("http server: %v", err)
		}
	}()

	<-ctx.Done()
	log.Printf("shutting down")
	shCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = srv.Shutdown(shCtx)
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
