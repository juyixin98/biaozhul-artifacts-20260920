package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

// Config holds all runtime configuration, populated from environment variables.
type Config struct {
	HTTPAddr      string
	MySQLDSN      string
	StorageRoot   string
	MaxUploadSize int64 // bytes
	BcryptCost    int
	RequestTO     time.Duration
}

func getenv(key, def string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return def
}

// Load reads configuration from the environment and validates it.
func Load() (*Config, error) {
	maxSize := int64(20 * 1024 * 1024)
	if v := os.Getenv("MAX_UPLOAD_BYTES"); v != "" {
		n, err := strconv.ParseInt(v, 10, 64)
		if err != nil || n < 64 {
			return nil, fmt.Errorf("MAX_UPLOAD_BYTES must be an integer >= 64, got %q", v)
		}
		maxSize = n
	}

	dsn := os.Getenv("MYSQL_DSN")
	if dsn == "" {
		dsn = fmt.Sprintf("%s:%s@tcp(%s:%s)/%s?charset=utf8mb4&parseTime=True&loc=UTC&multiStatements=true",
			getenv("MYSQL_USER", "proof"),
			getenv("MYSQL_PASSWORD", "proof"),
			getenv("MYSQL_HOST", "127.0.0.1"),
			getenv("MYSQL_PORT", "3306"),
			getenv("MYSQL_DATABASE", "proofcycle"),
		)
	}

	return &Config{
		HTTPAddr:      getenv("HTTP_ADDR", ":8080"),
		MySQLDSN:      dsn,
		StorageRoot:   getenv("STORAGE_ROOT", "./data/files"),
		MaxUploadSize: maxSize,
		BcryptCost:    10,
		RequestTO:     30 * time.Second,
	}, nil
}
