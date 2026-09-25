package tailsampling

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestConfigValidation(t *testing.T) {
	cases := []struct {
		name    string
		mutate  func(*Config)
		wantErr bool
	}{
		{"default is valid", func(c *Config) {}, false},
		{"zero wait window", func(c *Config) { c.WaitWindow = 0 }, true},
		{"ttl below wait", func(c *Config) { c.MaxTTL = c.WaitWindow - time.Millisecond }, true},
		{"rate above 1", func(c *Config) { c.ProbabilisticRate = 1.5 }, true},
		{"negative threshold", func(c *Config) { c.LatencyThresholdMs = -1 }, true},
		{"zero budget", func(c *Config) { c.BudgetCapacity = 0 }, true},
		{"empty data dir", func(c *Config) { c.DataDir = "" }, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			c := DefaultConfig()
			tc.mutate(&c)
			err := c.Validate()
			if (err != nil) != tc.wantErr {
				t.Fatalf("wantErr=%v got %v", tc.wantErr, err)
			}
		})
	}
}

func TestLoadConfigFileAndEnv(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "config.json")
	contents := `{
      "listen": "127.0.0.1:9999",
      "data_dir": "` + dir + `",
      "wait_window": "250ms",
      "max_ttl": "750ms",
      "latency_threshold_ms": 42,
      "probabilistic_rate": 0.25,
      "budget_capacity": 7,
      "budget_refill_per_sec": 3
    }`
	if err := os.WriteFile(p, []byte(contents), 0o644); err != nil {
		t.Fatal(err)
	}
	cfg, err := LoadConfigFile(p)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.WaitWindow != 250*time.Millisecond || cfg.MaxTTL != 750*time.Millisecond {
		t.Fatalf("durations not parsed: %+v", cfg)
	}
	if cfg.BudgetCapacity != 7 || cfg.ProbabilisticRate != 0.25 || cfg.LatencyThresholdMs != 42 {
		t.Fatalf("numeric values wrong: %+v", cfg)
	}

	t.Setenv("TS_WAIT_WINDOW", "500ms")
	t.Setenv("TS_MAX_TTL", "2s")
	t.Setenv("TS_BUDGET_CAPACITY", "9")
	if err := cfg.applyEnv(); err != nil {
		t.Fatal(err)
	}
	if cfg.WaitWindow != 500*time.Millisecond || cfg.BudgetCapacity != 9 {
		t.Fatalf("env override failed: %+v", cfg)
	}

	t.Setenv("TS_WAIT_WINDOW", "garbage")
	if err := cfg.applyEnv(); err == nil {
		t.Fatal("expected error for malformed TS_WAIT_WINDOW")
	}
}
