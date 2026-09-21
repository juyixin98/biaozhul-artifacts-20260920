// Package config loads runtime settings from environment variables.
package config

import (
	"os"
)

type Config struct {
	HTTPAddr      string
	DatabaseURL   string
	AdminAPIKey   string
	RunMigrations bool
	RunSeed       bool
}

func Load() Config {
	return Config{
		HTTPAddr:      env("HTTP_ADDR", ":8080"),
		DatabaseURL:   env("DATABASE_URL", "postgres://desklens:desklens@localhost:5432/desklens?sslmode=disable"),
		AdminAPIKey:   os.Getenv("ADMIN_API_KEY"),
		RunMigrations: env("RUN_MIGRATIONS", "true") != "false",
		RunSeed:       env("RUN_SEED", "true") != "false",
	}
}

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
