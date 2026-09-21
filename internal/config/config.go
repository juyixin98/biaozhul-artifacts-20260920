// Package config loads runtime configuration from the environment.
package config

import (
	"os"
	"time"
)

type Config struct {
	HTTPAddr       string
	MySQLDSN       string
	SweepInterval  time.Duration
	ReservationTTL time.Duration
}

func Load() Config {
	return Config{
		HTTPAddr:       getenv("HTTP_ADDR", ":8080"),
		MySQLDSN:       getenv("MYSQL_DSN", "root:rootpass@tcp(127.0.0.1:33306)/targetcraft?charset=utf8mb4&parseTime=true&loc=UTC&timeout=5s"),
		SweepInterval:  getdur("SWEEP_INTERVAL", 10*time.Second),
		ReservationTTL: getdur("RESERVATION_TTL", 5*time.Minute),
	}
}

func getenv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func getdur(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return def
}
