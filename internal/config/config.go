package config

import (
	"fmt"
	"os"
	"strconv"
	"time"
)

// Config holds process configuration loaded from environment variables.
type Config struct {
	DatabaseURL string
	HTTPAddr    string

	DataDir    string // root containing assets/ and outputs/
	AssetsDir  string
	OutputsDir string

	// Max 2 concurrent frame workers are allowed, per spec. The clamp is also
	// enforced in code; this field is post-clamp.
	WorkerConcurrency int
	LeaseTTL          time.Duration
	HeartbeatInterval time.Duration
	PollInterval      time.Duration
}

func Load() (Config, error) {
	cfg := Config{
		DatabaseURL:       getenv("DATABASE_URL", "postgres://vfx:vfx@localhost:5433/vfxqueue?sslmode=disable"),
		HTTPAddr:          getenv("HTTP_ADDR", ":8080"),
		DataDir:           getenv("DATA_DIR", "./data"),
		WorkerConcurrency: getenvInt("WORKER_CONCURRENCY", 2),
		LeaseTTL:          time.Duration(getenvInt("LEASE_TTL_SECONDS", 20)) * time.Second,
		HeartbeatInterval: time.Duration(getenvInt("HEARTBEAT_INTERVAL_SECONDS", 7)) * time.Second,
		PollInterval:      time.Duration(getenvInt("POLL_INTERVAL_MILLIS", 500)) * time.Millisecond,
	}
	cfg.AssetsDir = getenv("ASSETS_DIR", joinPath(cfg.DataDir, "assets"))
	cfg.OutputsDir = getenv("OUTPUTS_DIR", joinPath(cfg.DataDir, "outputs"))
	if cfg.WorkerConcurrency < 1 {
		return cfg, fmt.Errorf("WORKER_CONCURRENCY must be >= 1")
	}
	if cfg.WorkerConcurrency > 2 {
		cfg.WorkerConcurrency = 2
	}
	if cfg.LeaseTTL < 2*time.Second {
		return cfg, fmt.Errorf("LEASE_TTL_SECONDS too small")
	}
	return cfg, nil
}

func getenv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func getenvInt(k string, def int) int {
	if v := os.Getenv(k); v != "" {
		n, err := strconv.Atoi(v)
		if err == nil {
			return n
		}
	}
	return def
}

func joinPath(a, b string) string {
	if a == "" {
		return b
	}
	if a[len(a)-1] == '/' {
		return a + b
	}
	return a + "/" + b
}
