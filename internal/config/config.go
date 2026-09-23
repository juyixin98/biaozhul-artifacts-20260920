// Package config loads runtime configuration from environment variables.
package config

import (
	"fmt"
	"os"
)

// Config holds all process configuration.
type Config struct {
	HTTPAddr    string
	DatabaseURL string
}

// Load reads configuration from the environment, applying local defaults.
func Load() (Config, error) {
	cfg := Config{
		HTTPAddr:    envOr("XIMBOX_HTTP_ADDR", ":8080"),
		DatabaseURL: envOr("XIMBOX_DATABASE_URL", "postgres://ximbox:ximbox_dev_pwd@localhost:5432/ximbox?sslmode=disable"),
	}
	if cfg.DatabaseURL == "" {
		return Config{}, fmt.Errorf("XIMBOX_DATABASE_URL must not be empty")
	}
	return cfg, nil
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
