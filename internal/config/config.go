// Package config holds runtime configuration loaded from environment variables.
package config

import (
	"fmt"
	"os"
	"strconv"
	"time"
)

// Config is the resolved application configuration.
type Config struct {
	HTTPAddr string
	// MySQLDSN is the GORM/MySQL data source name.
	MySQLDSN string
	// Workers is the number of worker goroutines pulling jobs.
	Workers int
	// MaxBrowsers limits how many Chromium processes run concurrently.
	MaxBrowsers int
	// LeaseTTL is how long a claimed lease stays valid without a heartbeat.
	LeaseTTL time.Duration
	// HeartbeatInterval is how often a running worker refreshes its lease.
	HeartbeatInterval time.Duration
	// ReapInterval is how often the reaper looks for expired leases.
	ReapInterval time.Duration
	// MaxRedirects is the maximum number of HTTP redirects allowed during navigation.
	MaxRedirects int
	// NavTimeout is the hard timeout for a single page navigation.
	NavTimeout time.Duration
	// ChromeBin optionally pins the Chromium/Chrome executable path.
	ChromeBin string
	// DemoSiteURL, when set, is whitelisted and seeded on startup.
	DemoSiteURL string
	// TestSiteAddr is the bind address of the built-in test site (empty disables it).
	TestSiteAddr string
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

func getenvDur(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return def
}

// Load reads configuration from the environment, applying local defaults.
func Load() Config {
	host := getenv("SV_MYSQL_HOST", "127.0.0.1")
	port := getenv("SV_MYSQL_PORT", "3306")
	user := getenv("SV_MYSQL_USER", "root")
	pass := getenv("SV_MYSQL_PASSWORD", "")
	dbName := getenv("SV_MYSQL_DB", "sitevitals_b")
	dsn := fmt.Sprintf("%s:%s@tcp(%s:%s)/%s?charset=utf8mb4&parseTime=True&loc=UTC&multiStatements=true",
		user, pass, host, port, dbName)
	if v := os.Getenv("SV_MYSQL_DSN"); v != "" {
		dsn = v
	}
	return Config{
		HTTPAddr:          getenv("SV_HTTP_ADDR", ":8092"),
		MySQLDSN:          dsn,
		Workers:           getenvInt("SV_WORKERS", 4),
		MaxBrowsers:       getenvInt("SV_MAX_BROWSERS", 2),
		LeaseTTL:          getenvDur("SV_LEASE_TTL", 30*time.Second),
		HeartbeatInterval: getenvDur("SV_HEARTBEAT_INTERVAL", 10*time.Second),
		ReapInterval:      getenvDur("SV_REAP_INTERVAL", 5*time.Second),
		MaxRedirects:      getenvInt("SV_MAX_REDIRECTS", 5),
		NavTimeout:        getenvDur("SV_NAV_TIMEOUT", 45*time.Second),
		ChromeBin:         os.Getenv("SV_CHROME_BIN"),
		DemoSiteURL:       getenv("SV_DEMO_SITE_URL", ""),
		TestSiteAddr:      getenv("SV_TESTSITE_ADDR", ":8093"),
	}
}
