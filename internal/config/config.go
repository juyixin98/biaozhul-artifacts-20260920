package config

import (
	"os"
)

type Config struct {
	Port        string
	DatabaseURL string
	AdminToken  string
}

func Load() Config {
	return Config{
		Port:        env("PORT", "8080"),
		DatabaseURL: env("DATABASE_URL", "postgres://signalboard:signalboard@localhost:5432/signalboard?sslmode=disable"),
		AdminToken:  env("ADMIN_TOKEN", "dev-admin-token"),
	}
}

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
