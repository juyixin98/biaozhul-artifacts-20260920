package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"sitevitals/internal/browser"
)

// Config holds all runtime configuration, populated from environment
// variables with sensible local defaults.
type Config struct {
	HTTPAddr           string
	DemoAddr           string
	MySQLDSN           string
	ChromePath         string
	ChromiumWSURL      string // when set, reuse an external Chrome over CDP instead of launching
	Headless           bool
	BrowserConcurrency int
	LeaseDuration      time.Duration
	HeartbeatInterval  time.Duration
	PollInterval       time.Duration
	RunAfterBackoff    time.Duration
	NavTimeout         time.Duration
	TaskTimeout        time.Duration
	SettleTime         time.Duration
	MaxRedirects       int
	WorkerID           string
	EnableAPI          bool
	EnableWorker       bool
	EnableDemo         bool
	AutoMigrate        bool
	StrictSubresources bool
	RunOnce            bool // claim & process at most one task then exit (demo/CI)
}

func getenv(key, def string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return def
}

func getenvInt(key string, def int) int {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func getenvBool(key string, def bool) bool {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		switch strings.ToLower(v) {
		case "true", "1", "yes", "on":
			return true
		case "false", "0", "no", "off":
			return false
		}
	}
	return def
}

func getenvDur(key string, def time.Duration) time.Duration {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return def
}

// Load reads configuration from the environment.
func Load() Config {
	c := Config{
		HTTPAddr:           getenv("HTTP_ADDR", ":8080"),
		DemoAddr:           getenv("DEMO_ADDR", ":8090"),
		ChromePath:         os.Getenv("CHROME_PATH"), // chromedp auto-discovers when empty
		ChromiumWSURL:      os.Getenv("CHROMIUM_WS_URL"),
		Headless:           getenvBool("HEADLESS", true),
		BrowserConcurrency: getenvInt("BROWSER_CONCURRENCY", 2),
		LeaseDuration:      getenvDur("LEASE_DURATION", 90*time.Second),
		HeartbeatInterval:  getenvDur("HEARTBEAT_INTERVAL", 10*time.Second),
		PollInterval:       getenvDur("POLL_INTERVAL", 2*time.Second),
		RunAfterBackoff:    getenvDur("RETRY_BACKOFF", 10*time.Second),
		NavTimeout:         getenvDur("NAV_TIMEOUT", 30*time.Second),
		TaskTimeout:        getenvDur("TASK_TIMEOUT", 60*time.Second),
		SettleTime:         getenvDur("SETTLE_TIME", 3*time.Second),
		MaxRedirects:       getenvInt("MAX_REDIRECTS", browser.DefaultMaxRedirects),
		WorkerID:           getenv("WORKER_ID", ""),
		EnableAPI:          true,
		EnableWorker:       getenvBool("ENABLE_WORKER", true),
		EnableDemo:         getenvBool("ENABLE_DEMO", false),
		AutoMigrate:        getenvBool("AUTO_MIGRATE", true),
		StrictSubresources: getenvBool("STRICT_SUBRESOURCES", false),
		RunOnce:            getenvBool("WORKER_RUN_ONCE", false),
	}
	if c.WorkerID == "" {
		host, _ := os.Hostname()
		c.WorkerID = fmt.Sprintf("worker-%s-%d", host, os.Getpid())
	}

	// DSN: prefer a full DSN, otherwise assemble from MYSQL_* parts.
	if dsn := strings.TrimSpace(os.Getenv("MYSQL_DSN")); dsn != "" {
		c.MySQLDSN = dsn
	} else {
		user := getenv("MYSQL_USER", "sitevitals")
		pass := getenv("MYSQL_PASSWORD", "sitevitals")
		host := getenv("MYSQL_HOST", "127.0.0.1")
		port := getenv("MYSQL_PORT", "3306")
		db := getenv("MYSQL_DATABASE", "sitevitals")
		c.MySQLDSN = fmt.Sprintf(
			"%s:%s@tcp(%s:%s)/%s?charset=utf8mb4&parseTime=true&loc=UTC&multiStatements=true&timeout=5s",
			user, pass, host, port, db)
	}
	return c
}
