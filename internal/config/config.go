// Package config loads environment configuration with safe local defaults.
package config

import (
	"fmt"
	"os"
	"strconv"
	"time"
)

type Config struct {
	HTTPAddr       string
	DatabaseURL    string
	StubURL        string
	HMACKeyID      string
	HMACSecret     []byte
	ObservationMS  int64
	MinSamples     int64
	RequestTimeout time.Duration
}

func Load() (Config, error) {
	cfg := Config{
		HTTPAddr:       env("HTTP_ADDR", ":8080"),
		DatabaseURL:    env("DATABASE_URL", "postgres://rollout:rollout@localhost:5432/rollout?sslmode=disable"),
		StubURL:        env("STUB_URL", "http://localhost:8081"),
		HMACKeyID:      env("HMAC_KEY_ID", "local-dev-key"),
		HMACSecret:     []byte(env("HMAC_SECRET", "dev-shared-secret-change-me")),
		ObservationMS:  int64(envInt("OBSERVATION_MS", 2000)),
		MinSamples:     int64(envInt("MIN_SAMPLES", 80)),
		RequestTimeout: time.Duration(envInt("REQUEST_TIMEOUT_MS", 10000)) * time.Millisecond,
	}
	if len(cfg.HMACSecret) < 8 {
		return cfg, fmt.Errorf("HMAC_SECRET must be at least 8 bytes")
	}
	if cfg.ObservationMS <= 0 || cfg.MinSamples <= 0 {
		return cfg, fmt.Errorf("OBSERVATION_MS and MIN_SAMPLES must be positive")
	}
	return cfg, nil
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func envInt(k string, def int) int {
	if v := os.Getenv(k); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}
