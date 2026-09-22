package config

import (
	"os"
	"strconv"
	"time"
)

type Config struct {
	DatabaseURL string
	HTTPAddr    string
	JWTSecret   string
	JWTTTL      time.Duration
}

func Load() Config {
	c := Config{
		DatabaseURL: getenv("DATABASE_URL", "postgres://gov:gov@localhost:5432/gov?sslmode=disable"),
		HTTPAddr:    getenv("HTTP_ADDR", ":8080"),
		JWTSecret:   getenv("JWT_SECRET", "dev-insecure-secret-change-me"),
		JWTTTL:      24 * time.Hour,
	}
	if ttl := os.Getenv("JWT_TTL"); ttl != "" {
		if d, err := time.ParseDuration(ttl); err == nil {
			c.JWTTTL = d
		}
	}
	if n, err := strconv.Atoi(getenv("JWT_TTL_SECONDS", "0")); err == nil && n > 0 {
		c.JWTTTL = time.Duration(n) * time.Second
	}
	return c
}

func getenv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
