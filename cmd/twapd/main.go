// Command twapd starts the time-weighted average price service.
package main

import (
	"context"
	"encoding/hex"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	cryptopkg "twap/internal/crypto"
	"twap/internal/httpapi"
	"twap/internal/service"
	"twap/internal/storage"
)

func main() {
	if err := run(); err != nil {
		log.Fatalf("twapd: %v", err)
	}
}

func run() error {
	cfg, err := loadConfig()
	if err != nil {
		return err
	}

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	db, err := storage.New(ctx, cfg.databaseURL)
	if err != nil {
		return err
	}
	defer db.Close()
	if err := db.EnsureSchema(ctx); err != nil {
		return fmt.Errorf("migrate schema: %w", err)
	}
	log.Printf("database ready, schema ensured")

	svc := service.New(db, service.Config{
		WindowMicros:     cfg.windowMicros,
		MaxLateMicros:    cfg.maxLateMicros,
		StaleAfterMicros: cfg.staleAfterMicros,
		SigningKey:       cfg.signingKey,
	}, nil)

	srv := &http.Server{
		Addr:              cfg.listenAddr,
		Handler:           httpapi.NewRouter(svc, cfg.adminToken),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("twapd listening on %s (window=%s, max-late=%s, stale-after=%s)",
			cfg.listenAddr,
			time.Duration(cfg.windowMicros)*time.Microsecond,
			time.Duration(cfg.maxLateMicros)*time.Microsecond,
			time.Duration(cfg.staleAfterMicros)*time.Microsecond)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http server: %v", err)
		}
	}()

	<-ctx.Done()
	log.Printf("shutdown signal received")
	shutdownCtx, c := context.WithTimeout(context.Background(), 10*time.Second)
	defer c()
	return srv.Shutdown(shutdownCtx)
}

type appConfig struct {
	databaseURL      string
	listenAddr       string
	windowMicros     int64
	maxLateMicros    int64
	staleAfterMicros int64
	signingKey       []byte
	adminToken       string
}

func loadConfig() (appConfig, error) {
	var cfg appConfig
	cfg.databaseURL = getenv("DATABASE_URL",
		"postgres://twap_p015_a:twap_p015_a_pw@localhost:5432/twap_p015_a?sslmode=disable")
	cfg.listenAddr = getenv("LISTEN_ADDR", ":8080")

	window, err := getDuration("WINDOW", 60*time.Second)
	if err != nil {
		return cfg, err
	}
	maxLate, err := getDuration("MAX_LATE", 5*time.Minute)
	if err != nil {
		return cfg, err
	}
	staleAfter, err := getDuration("STALE_AFTER", 30*time.Second)
	if err != nil {
		return cfg, err
	}
	if window <= 0 {
		return cfg, errors.New("WINDOW must be positive")
	}
	if maxLate <= 0 {
		return cfg, errors.New("MAX_LATE must be positive")
	}
	cfg.windowMicros = int64(window / time.Microsecond)
	cfg.maxLateMicros = int64(maxLate / time.Microsecond)
	cfg.staleAfterMicros = int64(staleAfter / time.Microsecond)

	keyHex := os.Getenv("SIGNING_KEY")
	if strings.TrimSpace(keyHex) != "" {
		key, err := hex.DecodeString(keyHex)
		if err != nil {
			return cfg, fmt.Errorf("SIGNING_KEY must be hex: %w", err)
		}
		if len(key) < 16 {
			return cfg, errors.New("SIGNING_KEY must decode to at least 16 bytes")
		}
		cfg.signingKey = key
	} else {
		key, err := cryptopkg.GenerateKey()
		if err != nil {
			return cfg, err
		}
		cfg.signingKey = key
		log.Printf("SIGNING_KEY not set; generated ephemeral HMAC key %x (set SIGNING_KEY for stable signatures)", key)
	}

	cfg.adminToken = os.Getenv("ADMIN_TOKEN")
	if cfg.adminToken == "" {
		log.Printf("ADMIN_TOKEN not set; /v1/admin/recompute is disabled")
	}
	return cfg, nil
}

func getenv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func getDuration(key string, def time.Duration) (time.Duration, error) {
	v := os.Getenv(key)
	if v == "" {
		return def, nil
	}
	if n, err := strconv.ParseInt(v, 10, 64); err == nil {
		return time.Duration(n) * time.Microsecond, nil
	}
	d, err := time.ParseDuration(v)
	if err != nil {
		return 0, fmt.Errorf("%s: parse %q as duration (e.g. 60s or 60000000 micros): %w", key, v, err)
	}
	return d, nil
}
