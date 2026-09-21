package config

import (
	"os"
	"time"
)

// Config holds runtime configuration sourced from environment variables.
type Config struct {
	DatabaseURL string
	Addr        string
	// ClaimTTL is how long a moderator claim on a review task stays valid.
	ClaimTTL time.Duration
}

func Load() Config {
	c := Config{
		DatabaseURL: getenv("DATABASE_URL",
			"postgres://community:community@localhost:5432/communityvault?sslmode=disable"),
		Addr:     getenv("HTTP_ADDR", ":8080"),
		ClaimTTL: 60 * time.Second,
	}
	if d := os.Getenv("CLAIM_TTL_SECONDS"); d != "" {
		if secs, err := time.ParseDuration(d + "s"); err == nil {
			c.ClaimTTL = secs
		}
	}
	return c
}

func getenv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
