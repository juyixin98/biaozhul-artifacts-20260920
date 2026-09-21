package config

import (
	"os"
	"strconv"
	"time"
)

type Config struct {
	Port            string
	DatabaseDSN     string
	ReservationTTL  time.Duration // how long a reservation stays valid awaiting confirm
	SweepInterval   time.Duration // how often the expiry sweeper runs
	RunMigrations   bool
	SeedSampleData  bool
	ShutdownTimeout time.Duration
}

func FromEnv() Config {
	return Config{
		Port:            getEnv("PORT", "8080"),
		DatabaseDSN:     getEnv("DATABASE_DSN", "root:root@tcp(127.0.0.1:3306)/targetcraft?charset=utf8mb4&parseTime=true&loc=UTC"),
		ReservationTTL:  getEnvDuration("RESERVATION_TTL", 5*time.Minute),
		SweepInterval:   getEnvDuration("SWEEP_INTERVAL", time.Second),
		RunMigrations:   getEnvBool("RUN_MIGRATIONS", true),
		SeedSampleData:  getEnvBool("SEED_SAMPLE_DATA", false),
		ShutdownTimeout: getEnvDuration("SHUTDOWN_TIMEOUT", 5*time.Second),
	}
}

func getEnv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func getEnvBool(key string, def bool) bool {
	if v := os.Getenv(key); v != "" {
		if b, err := strconv.ParseBool(v); err == nil {
			return b
		}
	}
	return def
}

func getEnvDuration(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return def
}
