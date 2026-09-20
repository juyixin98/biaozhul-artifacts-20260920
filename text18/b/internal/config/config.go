package config

import (
	"fmt"
	"os"
	"time"
)

// Config holds runtime settings. Everything comes from the environment so the
// same image runs in docker compose and tests.
type Config struct {
	HTTPAddr          string
	DatabaseURL       string
	MigrationsDir     string
	ReminderInterval  time.Duration
	ReminderBatchSize int32
}

func Load() Config {
	return Config{
		HTTPAddr:          env("HTTP_ADDR", ":8080"),
		DatabaseURL:       env("DATABASE_URL", "postgres://sircc:sircc@localhost:5432/sircc?sslmode=disable"),
		MigrationsDir:     env("MIGRATIONS_DIR", "migrations"),
		ReminderInterval:  envDuration("REMINDER_INTERVAL", 10*time.Second),
		ReminderBatchSize: int32(envInt("REMINDER_BATCH_SIZE", 100)),
	}
}

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func envInt(key string, fallback int) int {
	if v := os.Getenv(key); v != "" {
		var n int
		if _, err := fmt.Sscanf(v, "%d", &n); err == nil {
			return n
		}
	}
	return fallback
}

func envDuration(key string, fallback time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return fallback
}
