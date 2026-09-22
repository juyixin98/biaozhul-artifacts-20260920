// Package config loads runtime configuration from environment variables.
package config

import (
	"fmt"
	"os"
	"strconv"
)

type Config struct {
	HTTPAddr       string
	DatabaseURL    string
	SeedOnStart    bool
	MigrateOnStart bool
}

func Load() (Config, error) {
	c := Config{
		HTTPAddr:       getenv("COSTLENS_HTTP_ADDR", ":8080"),
		DatabaseURL:    getenv("DATABASE_URL", "postgres://costlens:costlens@localhost:5432/costlens?sslmode=disable"),
		SeedOnStart:    getenv("COSTLENS_SEED", "false") == "true",
		MigrateOnStart: getenv("COSTLENS_AUTOMIGRATE", "true") == "true",
	}
	if c.DatabaseURL == "" {
		return c, fmt.Errorf("DATABASE_URL is required")
	}
	return c, nil
}

func getenv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// ParseLimitOffset parses common pagination query params with sane defaults.
func ParseLimitOffset(rawLimit, rawOffset string) (int32, int32) {
	limit := int64(100)
	offset := int64(0)
	if v, err := strconv.ParseInt(rawLimit, 10, 64); err == nil && v > 0 && v <= 10000 {
		limit = v
	}
	if v, err := strconv.ParseInt(rawOffset, 10, 64); err == nil && v >= 0 {
		offset = v
	}
	return int32(limit), int32(offset)
}
