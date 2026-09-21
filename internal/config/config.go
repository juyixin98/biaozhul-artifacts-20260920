package config

import (
	"fmt"
	"os"
)

type Config struct {
	Addr          string
	DatabaseURL   string
	MigrationsDir string
}

func Load() (Config, error) {
	cfg := Config{
		Addr:          env("DESKLENS_ADDR", ":8080"),
		DatabaseURL:   env("DATABASE_URL", "postgres://desklens:desklens@localhost:5432/desklens?sslmode=disable"),
		MigrationsDir: env("MIGRATIONS_DIR", "internal/database/migrations"),
	}
	if cfg.Addr == "" || cfg.DatabaseURL == "" {
		return Config{}, fmt.Errorf("missing required configuration")
	}
	return cfg, nil
}

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
