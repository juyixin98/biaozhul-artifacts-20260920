package config

import (
	"os"
	"time"
)

// Config holds runtime settings sourced from environment variables.
type Config struct {
	DatabaseURL string
	Addr        string
	// ClaimTTL is how long a moderator may hold a queue claim before it can
	// be recycled. Configurable so tests can exercise expiry deterministically.
	ClaimTTL       time.Duration
	RunMigrations  bool
	RunSeed        bool
	MigrationsPath string
	SeedPath       string
}

func getenv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func Load() Config {
	ttl, err := time.ParseDuration(getenv("CLAIM_TTL", "2m"))
	if err != nil || ttl <= 0 {
		ttl = 2 * time.Minute
	}
	return Config{
		DatabaseURL:    getenv("DATABASE_URL", "postgres://cv:cv@localhost:55440/communityvault?sslmode=disable"),
		Addr:           getenv("ADDR", ":8080"),
		ClaimTTL:       ttl,
		RunMigrations:  getenv("RUN_MIGRATIONS", "true") != "false",
		RunSeed:        getenv("RUN_SEED", "true") != "false",
		MigrationsPath: getenv("MIGRATIONS_PATH", "migrations"),
		SeedPath:       getenv("SEED_PATH", "seed/seed.sql"),
	}
}
