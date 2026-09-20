package config

import (
	"os"
	"time"
)

// Config holds runtime settings resolved from environment variables.
type Config struct {
	Addr               string
	DatabaseURL        string
	AdminToken         string
	ScreenOfflineAfter time.Duration
	ShutdownTimeout    time.Duration
}

func getenv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// Load reads configuration from the environment.
func Load() Config {
	offline := 90 * time.Second
	if v := os.Getenv("SCREEN_OFFLINE_AFTER"); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			offline = d
		}
	}
	shutdown := 15 * time.Second
	if v := os.Getenv("SHUTDOWN_TIMEOUT"); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			shutdown = d
		}
	}
	return Config{
		Addr:               getenv("ADDR", ":8080"),
		DatabaseURL:        getenv("DATABASE_URL", "postgres://signalboard:signalboard@localhost:5432/signalboard?sslmode=disable"),
		AdminToken:         getenv("ADMIN_TOKEN", "dev-admin-token"),
		ScreenOfflineAfter: offline,
		ShutdownTimeout:    shutdown,
	}
}
