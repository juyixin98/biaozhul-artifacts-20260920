package config

import (
	"fmt"
	"os"
	"strconv"
	"time"
)

// Config holds all runtime configuration, sourced from environment variables.
type Config struct {
	DatabaseURL string
	HTTPAddr    string

	// AgingPerSec is the default priority boost per second of waiting.
	AgingPerSec float64
	// SweepInterval is how often the timeout sweeper runs.
	SweepInterval time.Duration
	// EvidenceSecret optionally pins the HMAC signing key (base64 or raw
	// bytes). When empty a random key is generated once and persisted in
	// service_meta so signatures survive restarts.
	EvidenceSecret string
}

func Load() (Config, error) {
	cfg := Config{
		DatabaseURL:    env("DATABASE_URL", "postgres://deadlock:deadlock@localhost:55432/deadlock?sslmode=disable"),
		HTTPAddr:       env("HTTP_ADDR", ":8080"),
		AgingPerSec:    1.0,
		SweepInterval:  500 * time.Millisecond,
		EvidenceSecret: os.Getenv("EVIDENCE_SECRET"),
	}
	if v := os.Getenv("AGING_PER_SEC"); v != "" {
		f, err := strconv.ParseFloat(v, 64)
		if err != nil || f < 0 {
			return cfg, fmt.Errorf("AGING_PER_SEC must be a non-negative number, got %q", v)
		}
		cfg.AgingPerSec = f
	}
	if v := os.Getenv("SWEEP_INTERVAL"); v != "" {
		d, err := time.ParseDuration(v)
		if err != nil || d <= 0 {
			return cfg, fmt.Errorf("SWEEP_INTERVAL must be a positive duration, got %q", v)
		}
		cfg.SweepInterval = d
	}
	return cfg, nil
}

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
