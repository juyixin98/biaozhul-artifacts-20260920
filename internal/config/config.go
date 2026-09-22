package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

type Config struct {
	HTTPAddr string

	DBHost string
	DBPort string
	DBUser string
	DBPass string
	DBName string

	// MaxEventsPerBatch caps ingestion batches (requirement: at most 2000).
	MaxEventsPerBatch int
	// MaxFutureDelay rejects events too far in the future.
	MaxFutureDelay time.Duration
	// MaxBackfillAge is the explicit, allowed late/backfill horizon.
	// Events older than now-MaxBackfillAge are rejected.
	MaxBackfillAge time.Duration

	// Window / statistics parameters.
	BurstWindow     time.Duration
	BurstThreshold  int
	NightStartHour  int
	NightEndHour    int // exclusive; 6 means 20:00..05:59:59
	StatHistoryDays int
	StatZScore      float64
	StatMinSamples  int
	EscalationAge   time.Duration

	// Scheduler periods.
	ProcessInterval  time.Duration
	SweepInterval    time.Duration
	EscalateInterval time.Duration
	ProcessBatch     int
	LockTimeout      time.Duration

	// Seed keys (only used when seeding an empty database).
	AdminKey   string
	AnalystKey string
}

func getenv(k, def string) string {
	if v := strings.TrimSpace(os.Getenv(k)); v != "" {
		return v
	}
	return def
}

func getdur(k string, def time.Duration) time.Duration {
	v := getenv(k, "")
	if v == "" {
		return def
	}
	d, err := time.ParseDuration(v)
	if err != nil {
		panic(fmt.Sprintf("invalid duration %s=%q: %v", k, v, err))
	}
	return d
}

func getint(k string, def int) int {
	v := getenv(k, "")
	if v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		panic(fmt.Sprintf("invalid int %s=%q: %v", k, v, err))
	}
	return n
}

func getfloat(k string, def float64) float64 {
	v := getenv(k, "")
	if v == "" {
		return def
	}
	f, err := strconv.ParseFloat(v, 64)
	if err != nil {
		panic(fmt.Sprintf("invalid float %s=%q: %v", k, v, err))
	}
	return f
}

// Load reads configuration from environment variables.
func Load() Config {
	return Config{
		HTTPAddr: getenv("HTTP_ADDR", ":8080"),

		DBHost: getenv("DB_HOST", "127.0.0.1"),
		DBPort: getenv("DB_PORT", "3306"),
		DBUser: getenv("DB_USER", "anomaly"),
		DBPass: getenv("DB_PASSWORD", "anomaly"),
		DBName: getenv("DB_NAME", "anomalywatch"),

		MaxEventsPerBatch: getint("MAX_EVENTS_PER_BATCH", 2000),
		MaxFutureDelay:    getdur("MAX_FUTURE_DELAY", 5*time.Minute),
		MaxBackfillAge:    getdur("MAX_BACKFILL_AGE", 30*24*time.Hour),

		BurstWindow:     getdur("BURST_WINDOW", 10*time.Minute),
		BurstThreshold:  getint("BURST_THRESHOLD", 50),
		NightStartHour:  getint("NIGHT_START_HOUR", 20),
		NightEndHour:    getint("NIGHT_END_HOUR", 6),
		StatHistoryDays: getint("STAT_HISTORY_DAYS", 30),
		StatZScore:      getfloat("STAT_ZSCORE", 2.5),
		StatMinSamples:  getint("STAT_MIN_SAMPLES", 10),
		EscalationAge:   getdur("ESCALATION_AGE", 24*time.Hour),

		ProcessInterval:  getdur("PROCESS_INTERVAL", 5*time.Second),
		SweepInterval:    getdur("SWEEP_INTERVAL", time.Hour),
		EscalateInterval: getdur("ESCALATE_INTERVAL", time.Minute),
		ProcessBatch:     getint("PROCESS_BATCH", 500),
		LockTimeout:      getdur("LOCK_TIMEOUT", 30*time.Second),

		AdminKey:   getenv("ADMIN_API_KEY", "admin-local-key"),
		AnalystKey: getenv("ANALYST_API_KEY", "analyst-local-key"),
	}
}

// DSN builds the MySQL DSN. multiStatements is required by the migration runner.
func (c Config) DSN(multiStatements bool) string {
	dsn := fmt.Sprintf("%s:%s@tcp(%s:%s)/%s?charset=utf8mb4&parseTime=true&loc=UTC&timeout=5s",
		c.DBUser, c.DBPass, c.DBHost, c.DBPort, c.DBName)
	if multiStatements {
		dsn += "&multiStatements=true"
	}
	return dsn
}
