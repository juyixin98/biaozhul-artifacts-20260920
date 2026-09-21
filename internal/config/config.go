package config

import (
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"
)

// Principal is an authenticated API caller.
type Principal struct {
	Name string
	Role string
}

const (
	RoleInvestigator = "investigator" // 登记、移交、备注、查询
	RoleAnalyst      = "analyst"      // 仅查询、备注
)

// Config holds all runtime configuration, loaded from environment variables.
type Config struct {
	Listen         string
	DBDriver       string // "mysql" in production; tests may use "sqlite"
	DSN            string
	EvidenceRoot   string // whitelist directory; only files inside it may be registered
	ChunkSize      int    // verification chunk size in bytes
	PollIntervalMS int
	// Principals maps a bearer token to its principal.
	Principals map[string]Principal
}

func getenv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// Load reads configuration from the environment.
func Load() (*Config, error) {
	cfg := &Config{
		Listen:         getenv("FORENSIC_LISTEN", ":8080"),
		DBDriver:       getenv("FORENSIC_DB_DRIVER", "mysql"),
		DSN:            os.Getenv("FORENSIC_DB_DSN"),
		EvidenceRoot:   getenv("FORENSIC_EVIDENCE_ROOT", "/data/evidence"),
		ChunkSize:      4 * 1024 * 1024,
		PollIntervalMS: 500,
		Principals:     map[string]Principal{},
	}
	if cfg.DSN == "" {
		return nil, errors.New("FORENSIC_DB_DSN is required")
	}
	if v := os.Getenv("FORENSIC_CHUNK_SIZE"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n <= 0 {
			return nil, fmt.Errorf("invalid FORENSIC_CHUNK_SIZE: %q", v)
		}
		cfg.ChunkSize = n
	}
	if v := os.Getenv("FORENSIC_POLL_INTERVAL_MS"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n <= 0 {
			return nil, fmt.Errorf("invalid FORENSIC_POLL_INTERVAL_MS: %q", v)
		}
		cfg.PollIntervalMS = n
	}

	// FORENSIC_AUTH entries: name:role:token, comma separated.
	// Example: alice:investigator:s3cret,bob:analyst:other
	authSpec := os.Getenv("FORENSIC_AUTH")
	if authSpec == "" {
		return nil, errors.New("FORENSIC_AUTH is required (format: name:role:token,name:role:token)")
	}
	for _, entry := range strings.Split(authSpec, ",") {
		entry = strings.TrimSpace(entry)
		if entry == "" {
			continue
		}
		parts := strings.SplitN(entry, ":", 3)
		if len(parts) != 3 || parts[0] == "" || parts[2] == "" {
			return nil, fmt.Errorf("invalid FORENSIC_AUTH entry: %q", entry)
		}
		role := strings.TrimSpace(parts[1])
		if role != RoleInvestigator && role != RoleAnalyst {
			return nil, fmt.Errorf("invalid role %q in FORENSIC_AUTH entry %q", role, entry)
		}
		if _, exists := cfg.Principals[parts[2]]; exists {
			return nil, fmt.Errorf("duplicate token in FORENSIC_AUTH entry %q", entry)
		}
		cfg.Principals[parts[2]] = Principal{Name: strings.TrimSpace(parts[0]), Role: role}
	}
	if _, err := cfg.requireRole(RoleInvestigator); err != nil {
		return nil, err
	}
	return cfg, nil
}

func (c *Config) requireRole(role string) (Principal, error) {
	for _, p := range c.Principals {
		if p.Role == role {
			return p, nil
		}
	}
	return Principal{}, fmt.Errorf("FORENSIC_AUTH must define at least one %s token", role)
}

// InvestigatorToken / AnalystToken are convenience helpers for tests and tooling.
func (c *Config) InvestigatorToken() (string, error) { return c.tokenFor(RoleInvestigator) }
func (c *Config) AnalystToken() (string, error)      { return c.tokenFor(RoleAnalyst) }

func (c *Config) tokenFor(role string) (string, error) {
	for tok, p := range c.Principals {
		if p.Role == role {
			return tok, nil
		}
	}
	return "", fmt.Errorf("no %s token configured", role)
}
