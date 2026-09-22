package config

import (
	"fmt"
	"os"
	"strconv"
	"time"
)

// Config holds all runtime configuration, loaded from environment variables.
type Config struct {
	HTTPAddr      string
	MySQLDSN      string
	MigrationDir  string
	AutoMigrate   bool
	SeedAPIKeys   string // comma-separated org=key pairs
	WorkerEnabled bool
	WorkerTick    time.Duration
	BatchSize     int
	LockTimeoutS  int
	// StaleJobAfter is how long a job may stay RUNNING (no heartbeat) before
	// another worker may reclaim it (crash recovery).
	StaleJobAfter time.Duration
}

// Load reads configuration from the environment, applying safe defaults that
// work with the supplied docker-compose MySQL service.
func Load() Config {
	dsn := getenv("MYSQL_DSN", "root:root@tcp(127.0.0.1:3306)/geoterritory?charset=utf8mb4&parseTime=true&loc=UTC&multiStatements=true")
	c := Config{
		HTTPAddr:      getenv("HTTP_ADDR", ":8080"),
		MySQLDSN:      dsn,
		MigrationDir:  getenv("MIGRATION_DIR", "migrations"),
		AutoMigrate:   getenvBool("AUTO_MIGRATE", true),
		SeedAPIKeys:   getenv("SEED_API_KEYS", "org-a=demo-key-a,org-b=demo-key-b"),
		WorkerEnabled: getenvBool("WORKER_ENABLED", true),
		WorkerTick:    time.Duration(getenvInt("WORKER_TICK_MS", 500)) * time.Millisecond,
		BatchSize:     getenvInt("REASSIGN_BATCH_SIZE", 500),
		LockTimeoutS:  getenvInt("ORG_LOCK_TIMEOUT_SECONDS", 15),
		StaleJobAfter: time.Duration(getenvInt("STALE_JOB_SECONDS", 30)) * time.Second,
	}
	return c
}

func getenv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func getenvBool(k string, def bool) bool {
	if v := os.Getenv(k); v != "" {
		b, err := strconv.ParseBool(v)
		if err != nil {
			panic(fmt.Sprintf("invalid bool for %s: %s", k, v))
		}
		return b
	}
	return def
}

func getenvInt(k string, def int) int {
	if v := os.Getenv(k); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil {
			panic(fmt.Sprintf("invalid int for %s: %s", k, v))
		}
		return n
	}
	return def
}
