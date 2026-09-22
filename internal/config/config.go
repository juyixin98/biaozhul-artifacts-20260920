// Package config loads DAMS runtime configuration from the environment.
package config

import (
	"fmt"
	"os"
)

type Config struct {
	HTTPAddr    string
	DatabaseURL string
	// MigrateOnStart applies embedded migrations before serving.
	MigrateOnStart bool
}

func Load() Config {
	c := Config{
		HTTPAddr:       env("DAMS_HTTP_ADDR", ":8080"),
		DatabaseURL:    env("DAMS_DATABASE_URL", "postgres://dams:dams@localhost:5432/dams?sslmode=disable"),
		MigrateOnStart: env("DAMS_MIGRATE_ON_START", "true") != "false",
	}
	if c.DatabaseURL == "" {
		fmt.Fprintln(os.Stderr, "DAMS_DATABASE_URL is required")
		os.Exit(2)
	}
	return c
}

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
