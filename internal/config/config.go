package config

import (
	"os"
)

type Config struct {
	HTTPAddr string
	Database string
	// PlatformBootstrapToken, when set, is accepted as a synthetic platform
	// admin token used only to create the first community (and further ones).
	// Empty (default) disables it.
	PlatformBootstrapToken string
}

func Load() Config {
	c := Config{
		HTTPAddr:               env("HTTP_ADDR", ":8080"),
		Database:               env("DATABASE_URL", "postgres://gov:gov@localhost:5432/gov?sslmode=disable"),
		PlatformBootstrapToken: env("PLATFORM_BOOTSTRAP_TOKEN", ""),
	}
	return c
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
