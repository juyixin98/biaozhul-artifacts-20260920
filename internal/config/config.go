package config

import (
	"os"
)

type Config struct {
	DatabaseURL string
	HTTPAddr    string
	SeedDemo    bool
}

func Load() Config {
	return Config{
		DatabaseURL: env("CLEARSETTLE_DATABASE_URL",
			"postgres://clearsettle:clearsettle@localhost:5433/clearsettle?sslmode=disable"),
		HTTPAddr: env("CLEARSETTLE_HTTP_ADDR", ":8080"),
		SeedDemo: os.Getenv("CLEARSETTLE_SEED_DEMO") == "true",
	}
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
