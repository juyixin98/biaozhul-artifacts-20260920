package config

import (
	"os"
	"time"
)

// Config holds all runtime configuration. Everything has a sensible default so
// the app boots out of the box; production deployments override via env.
type Config struct {
	HTTPAddr      string
	DatabaseURL   string
	CodecKeyHex   string // 32-byte AES-256 key, hex encoded (64 chars)
	SweepInterval time.Duration
	PollInterval  time.Duration

	// Lifecycle durations. All wall-clock waiting is expressed here so tests
	// can drive the engine deterministically.
	ApprovalWindow time.Duration // seller must approve/reject the transfer
	ExpiredGrace   time.Duration // registered -> expired
	RedeemPeriod   time.Duration // expired -> redeemable
	PendingDelete  time.Duration // redeemable -> pending_delete
}

func getenv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func durationEnv(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return def
}

// Load reads configuration from the environment.
func Load() Config {
	return Config{
		HTTPAddr:       getenv("HTTP_ADDR", ":8080"),
		DatabaseURL:    getenv("DATABASE_URL", "postgres://domain:domain@localhost:5432/domain?sslmode=disable"),
		CodecKeyHex:    getenv("CODEC_KEY_HEX", "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"),
		SweepInterval:  durationEnv("SWEEP_INTERVAL", 10*time.Second),
		PollInterval:   durationEnv("POLL_INTERVAL", 5*time.Second),
		ApprovalWindow: durationEnv("APPROVAL_WINDOW", 5*24*time.Hour),
		ExpiredGrace:   durationEnv("EXPIRED_GRACE", 0),
		RedeemPeriod:   durationEnv("REDEEM_PERIOD", 30*24*time.Hour),
		PendingDelete:  durationEnv("PENDING_DELETE", 5*24*time.Hour),
	}
}
