package config

import (
	"fmt"
	"os"
	"strings"
)

// Config holds runtime configuration loaded from environment variables.
type Config struct {
	// HTTPListen is the bind address for the Gin HTTP server.
	HTTPListen string
	// DBDriver is either "mysql" or "sqlite".
	DBDriver string
	// DSN is the driver specific data source name.
	DSN string
	// WhitelistRoots are absolute, real directories inside which evidence
	// images must live. Symlink escapes outside any root are rejected.
	WhitelistRoots []string
	// ChunkSize is the read chunk size in bytes used while hashing.
	ChunkSize int
	// Auth tokens for the two supported roles.
	InvestigatorToken string
	AnalystToken      string
	// SamplesDir is only used by the bundled sample generator target.
	SamplesDir string
}

// Load reads configuration from the environment and validates it.
func Load() (Config, error) {
	cfg := Config{
		HTTPListen:        env("HTTP_LISTEN", ":8080"),
		DBDriver:          env("DB_DRIVER", "mysql"),
		DSN:               env("DB_DSN", "forensic:forensic@tcp(mysql:3306)/forensiccore?charset=utf8mb4&parseTime=True&loc=Local"),
		InvestigatorToken: env("INVESTIGATOR_TOKEN", ""),
		AnalystToken:      env("ANALYST_TOKEN", ""),
		SamplesDir:        env("SAMPLES_DIR", "/data/samples"),
		ChunkSize:         envInt("HASH_CHUNK_SIZE", 4*1024*1024),
	}
	if cfg.DBDriver != "mysql" && cfg.DBDriver != "sqlite" {
		return cfg, fmt.Errorf("unsupported DB_DRIVER %q, want mysql or sqlite", cfg.DBDriver)
	}
	if cfg.DBDriver == "sqlite" && cfg.DSN == "" {
		return cfg, fmt.Errorf("DB_DSN must point at a sqlite file when DB_DRIVER=sqlite")
	}
	if cfg.ChunkSize < 4096 {
		return cfg, fmt.Errorf("HASH_CHUNK_SIZE must be >= 4096, got %d", cfg.ChunkSize)
	}

	roots := splitDirs(env("WHITELIST_DIRS", cfg.SamplesDir))
	if len(roots) == 0 {
		return cfg, fmt.Errorf("WHITELIST_DIRS must contain at least one directory")
	}
	for i, r := range roots {
		real, err := filepathEvalSymlinks(r)
		if err != nil {
			return cfg, fmt.Errorf("whitelist directory %q is not accessible: %w", r, err)
		}
		roots[i] = real
	}
	cfg.WhitelistRoots = roots

	if cfg.InvestigatorToken == "" {
		return cfg, fmt.Errorf("INVESTIGATOR_TOKEN must be set (do not use a blank token in production)")
	}
	if cfg.AnalystToken == "" {
		return cfg, fmt.Errorf("ANALYST_TOKEN must be set (do not use a blank token in production)")
	}
	if cfg.InvestigatorToken == cfg.AnalystToken {
		return cfg, fmt.Errorf("INVESTIGATOR_TOKEN and ANALYST_TOKEN must differ")
	}
	return cfg, nil
}

// HasRoot reports whether the given real path is inside any whitelist root.
func (c Config) HasRoot(realPath string) bool {
	for _, root := range c.WhitelistRoots {
		if pathWithin(realPath, root) {
			return true
		}
	}
	return false
}

func env(key, def string) string {
	if v, ok := os.LookupEnv(key); ok && v != "" {
		return v
	}
	return def
}

func envInt(key string, def int) int {
	if v, ok := os.LookupEnv(key); ok && v != "" {
		var n int
		if _, err := fmt.Sscanf(v, "%d", &n); err != nil || n <= 0 {
			return def
		}
		return n
	}
	return def
}

func splitDirs(s string) []string {
	parts := strings.Split(s, ":")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}
