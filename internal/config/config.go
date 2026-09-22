// Package config loads runtime configuration from environment variables.
package config

import (
	"fmt"
	"os"
	"strconv"
)

type Config struct {
	HTTPAddr     string
	DatabaseURL  string
	DataDir      string
	Workers      int
	LeaseSeconds int
	RenewSeconds int
	PollMillis   int
	// Bootstrap users, comma-separated "username:role:apikey" triples.
	BootstrapUsers []BootstrapUser
}

type BootstrapUser struct {
	Username string
	Role     string
	APIKey   string
}

func Load() (Config, error) {
	c := Config{
		HTTPAddr:     getenv("HTTP_ADDR", ":8080"),
		DatabaseURL:  getenv("DATABASE_URL", "postgres://renderq:renderq@localhost:5432/renderq?sslmode=disable"),
		DataDir:      getenv("DATA_DIR", "./data"),
		Workers:      getenvInt("WORKERS", 2),
		LeaseSeconds: getenvInt("LEASE_SECONDS", 30),
		RenewSeconds: getenvInt("RENEW_SECONDS", 10),
		PollMillis:   getenvInt("POLL_MILLIS", 200),
	}
	c.BootstrapUsers = []BootstrapUser{
		{Username: "admin", Role: "admin", APIKey: getenv("ADMIN_API_KEY", "dev-admin-key")},
		{Username: "alice", Role: "member", APIKey: getenv("ALICE_API_KEY", "dev-alice-key")},
		{Username: "bob", Role: "member", APIKey: getenv("BOB_API_KEY", "dev-bob-key")},
	}
	if c.Workers < 1 || c.Workers > 2 {
		return c, fmt.Errorf("WORKERS must be 1 or 2 (hard concurrency cap is 2), got %d", c.Workers)
	}
	if c.LeaseSeconds <= c.RenewSeconds {
		return c, fmt.Errorf("LEASE_SECONDS (%d) must be greater than RENEW_SECONDS (%d)", c.LeaseSeconds, c.RenewSeconds)
	}
	return c, nil
}

func getenv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func getenvInt(k string, def int) int {
	if v := os.Getenv(k); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil {
			return def
		}
		return n
	}
	return def
}
