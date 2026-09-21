package config

import (
	"os"
	"time"
)

type Config struct {
	HTTPAddr      string
	DatabaseURL   string
	JWTSecret     string
	WorkerLockID  string
	Bootstrap     Bootstrap
	AuthorizeTTL  time.Duration
	SettleHorizon time.Duration // captures older than this relative to now are settleable
}

type Bootstrap struct {
	AdminEmail    string
	AdminPassword string
}

func Get() Config {
	c := Config{
		HTTPAddr:      env("HTTP_ADDR", ":8080"),
		DatabaseURL:   env("DATABASE_URL", "postgres://clearsettle:clearsettle@localhost:5432/clearsettle?sslmode=disable"),
		JWTSecret:     env("JWT_SECRET", "dev-secret-change-me"),
		WorkerLockID:  env("WORKER_LOCK_ID", "worker-1"),
		AuthorizeTTL:  24 * time.Hour,
		SettleHorizon: envDuration("SETTLE_HORIZON", 24*time.Hour),
	}
	c.Bootstrap = Bootstrap{
		AdminEmail:    env("BOOTSTRAP_ADMIN_EMAIL", "admin@clearsettle.local"),
		AdminPassword: env("BOOTSTRAP_ADMIN_PASSWORD", "admin12345"),
	}
	return c
}

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envDuration(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return def
}
