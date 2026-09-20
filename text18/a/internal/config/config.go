package config

import (
	"os"
	"time"
)

type Config struct {
	HTTPAddr          string
	DatabaseURL       string
	SchedulerInterval time.Duration
	SweepBatchSize    int32
}

func getenv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func Load() Config {
	c := Config{
		HTTPAddr:          getenv("SIRCC_HTTP_ADDR", ":8080"),
		DatabaseURL:       getenv("SIRCC_DATABASE_URL", "postgres://sircc:sircc@localhost:5432/sircc?sslmode=disable"),
		SchedulerInterval: 10 * time.Second,
		SweepBatchSize:    100,
	}
	if d := os.Getenv("SIRCC_SCHEDULER_INTERVAL"); d != "" {
		if parsed, err := time.ParseDuration(d); err == nil {
			c.SchedulerInterval = parsed
		}
	}
	return c
}
