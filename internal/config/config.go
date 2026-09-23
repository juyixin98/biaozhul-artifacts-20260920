// Package config holds process configuration sourced from environment variables.
package config

import (
	"os"
	"time"
)

type Config struct {
	DatabaseURL string
	Addr        string
	Location    *time.Location
}

func Load() (Config, error) {
	dbURL := env("COSTLENS_DATABASE_URL",
		"postgres://costlens:costlens@localhost:5432/costlens?sslmode=disable")
	loc, err := time.LoadLocation(env("COSTLENS_TZ", "UTC"))
	if err != nil {
		return Config{}, err
	}
	return Config{
		DatabaseURL: dbURL,
		Addr:        env("COSTLENS_HTTP_ADDR", ":8080"),
		Location:    loc,
	}, nil
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
