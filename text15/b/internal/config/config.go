package config

import (
	"os"
	"strconv"
	"time"
)

// Config holds all runtime configuration, sourced from environment variables.
type Config struct {
	HTTPAddr      string
	MySQLDSN      string
	StorageDir    string
	MaxUploadSize int64 // bytes
	SeedDemo      bool
	DBRetryDelay  time.Duration
}

func getenv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func getenvInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

// Load reads configuration from the PROOFCYCLE_* environment variables.
func Load() Config {
	return Config{
		HTTPAddr: getenv("PROOFCYCLE_HTTP_ADDR", ":8080"),
		MySQLDSN: getenv(
			"PROOFCYCLE_MYSQL_DSN",
			"proofcycle:proofcycle_pw@tcp(127.0.0.1:3306)/proofcycle?charset=utf8mb4&parseTime=true&loc=UTC",
		),
		StorageDir:    getenv("PROOFCYCLE_STORAGE_DIR", "./data/storage"),
		MaxUploadSize: int64(getenvInt("PROOFCYCLE_MAX_UPLOAD_MB", 20)) * 1024 * 1024,
		SeedDemo:      os.Getenv("PROOFCYCLE_SEED_DEMO") == "1" || os.Getenv("PROOFCYCLE_SEED_DEMO") == "true",
		DBRetryDelay:  2 * time.Second,
	}
}
