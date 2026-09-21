package config

import (
	"fmt"
	"os"
	"strconv"
)

// Config holds all runtime configuration, loaded from environment variables.
type Config struct {
	HTTPAddr      string
	DatabaseURL   string
	DataDir       string
	MaxChunkBytes int64
	MaxJSONBytes  int64
}

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envInt64(key string, def int64) int64 {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil {
			return n
		}
	}
	return def
}

// Load reads configuration from the environment and applies defaults.
func Load() (Config, error) {
	cfg := Config{
		HTTPAddr:      env("SYN_HTTP_ADDR", ":8080"),
		DatabaseURL:   env("SYN_DATABASE_URL", "postgres://synapticgo:synapticgo@localhost:5432/synapticgo?sslmode=disable"),
		DataDir:       env("SYN_DATA_DIR", "./data"),
		MaxChunkBytes: envInt64("SYN_MAX_CHUNK_BYTES", 64<<20),
		MaxJSONBytes:  envInt64("SYN_MAX_JSON_BYTES", 8<<20),
	}
	if cfg.MaxChunkBytes <= 0 {
		return cfg, fmt.Errorf("SYN_MAX_CHUNK_BYTES must be positive")
	}
	if cfg.MaxJSONBytes <= 0 {
		return cfg, fmt.Errorf("SYN_MAX_JSON_BYTES must be positive")
	}
	return cfg, nil
}

// LoadAddrForHealth returns only the listen address, for the container
// healthcheck probe which must not connect to the database.
func LoadAddrForHealth() string {
	return env("SYN_HTTP_ADDR", ":8080")
}
