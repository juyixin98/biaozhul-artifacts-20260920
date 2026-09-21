package config

import (
	"fmt"
	"os"
	"strconv"
)

// Config holds all runtime settings, sourced from environment variables.
type Config struct {
	HTTPAddr     string
	DatabaseURL  string
	DataDir      string
	MaxBodyBytes int64
}

func Load() (Config, error) {
	cfg := Config{
		HTTPAddr:     getenv("HTTP_ADDR", ":8080"),
		DatabaseURL:  getenv("DATABASE_URL", "postgres://synaptic:synaptic@localhost:5432/synapticgo?sslmode=disable"),
		DataDir:      getenv("DATA_DIR", "./data"),
		MaxBodyBytes: 256 << 20,
	}
	if v := os.Getenv("MAX_BODY_BYTES"); v != "" {
		n, err := strconv.ParseInt(v, 10, 64)
		if err != nil || n <= 0 {
			return Config{}, fmt.Errorf("MAX_BODY_BYTES must be a positive integer")
		}
		cfg.MaxBodyBytes = n
	}
	return cfg, nil
}

func getenv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
