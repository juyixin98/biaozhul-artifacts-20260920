package tailsampling

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strconv"
	"time"
)

// Status constants.
const (
	StatusOK    = "OK"
	StatusError = "ERROR"
)

// Config controls the sampler: decision wait window, degradation TTL,
// policies and keep budget.
type Config struct {
	// Listen is the HTTP listen address.
	Listen string `json:"listen"`
	// DataDir holds the JSONL decision log, late-arrival log and snapshots.
	DataDir string `json:"data_dir"`

	// WaitWindow is how long a complete trace is held before the decision
	// is finalized, to absorb late spans ("decision wait window").
	WaitWindow time.Duration
	// MaxTTL is the hard upper bound a trace may buffer: when exceeded the
	// trace is finalized while possibly incomplete and explicitly marked.
	MaxTTL time.Duration

	// SnapshotInterval is how often open traces are snapshotted to disk.
	SnapshotInterval time.Duration

	// ErrorPolicy keeps every trace containing an ERROR span.
	ErrorPolicy bool `json:"error_policy"`
	// LatencyThresholdMs: a trace whose total duration meets/exceeds this is
	// a tail-latency keep candidate.
	LatencyThresholdMs int64 `json:"latency_threshold_ms"`
	// ProbabilisticRate is the baseline keep fraction for everything else.
	ProbabilisticRate float64 `json:"probabilistic_rate"`

	// BudgetCapacity is the keep-budget token bucket size (kept traces).
	BudgetCapacity float64 `json:"budget_capacity"`
	// BudgetRefillPerSec refills the bucket at this rate (kept traces/sec).
	BudgetRefillPerSec float64 `json:"budget_refill_per_sec"`

	// String forms used by JSON loading and -help output.
	WaitWindowString       string `json:"wait_window"`
	MaxTTLString           string `json:"max_ttl"`
	SnapshotIntervalString string `json:"snapshot_interval"`
}

// DefaultConfig returns a configuration suited to local demos: short windows,
// a generous latency threshold and a small refillable budget.
func DefaultConfig() Config {
	return Config{
		Listen:                 ":8080",
		DataDir:                "./data",
		WaitWindow:             2 * time.Second,
		MaxTTL:                 10 * time.Second,
		SnapshotInterval:       5 * time.Second,
		ErrorPolicy:            true,
		LatencyThresholdMs:     1000,
		ProbabilisticRate:      0.10,
		BudgetCapacity:         100,
		BudgetRefillPerSec:     10,
		WaitWindowString:       "2s",
		MaxTTLString:           "10s",
		SnapshotIntervalString: "5s",
	}
}

func (c *Config) normalizeDurations() error {
	if c.WaitWindowString != "" {
		d, err := time.ParseDuration(c.WaitWindowString)
		if err != nil {
			return fmt.Errorf("wait_window: %w", err)
		}
		c.WaitWindow = d
	}
	if c.MaxTTLString != "" {
		d, err := time.ParseDuration(c.MaxTTLString)
		if err != nil {
			return fmt.Errorf("max_ttl: %w", err)
		}
		c.MaxTTL = d
	}
	if c.SnapshotIntervalString != "" {
		d, err := time.ParseDuration(c.SnapshotIntervalString)
		if err != nil {
			return fmt.Errorf("snapshot_interval: %w", err)
		}
		c.SnapshotInterval = d
	}
	return nil
}

// Validate checks configuration consistency.
func (c *Config) Validate() error {
	if c.WaitWindow <= 0 {
		return errors.New("wait_window must be > 0")
	}
	if c.MaxTTL < c.WaitWindow {
		return errors.New("max_ttl must be >= wait_window")
	}
	if c.LatencyThresholdMs < 0 {
		return errors.New("latency_threshold_ms must be >= 0")
	}
	if c.ProbabilisticRate < 0 || c.ProbabilisticRate > 1 {
		return errors.New("probabilistic_rate must be within [0,1]")
	}
	if c.BudgetCapacity <= 0 {
		return errors.New("budget_capacity must be > 0")
	}
	if c.BudgetRefillPerSec < 0 {
		return errors.New("budget_refill_per_sec must be >= 0")
	}
	if c.DataDir == "" {
		return errors.New("data_dir must be set")
	}
	return nil
}

// LoadConfigFile parses a JSON config file over default values.
func LoadConfigFile(path string) (Config, error) {
	cfg := DefaultConfig()
	b, err := os.ReadFile(path)
	if err != nil {
		return cfg, err
	}
	if err := json.Unmarshal(b, &cfg); err != nil {
		return cfg, fmt.Errorf("parse %s: %w", path, err)
	}
	if err := cfg.normalizeDurations(); err != nil {
		return cfg, err
	}
	return cfg, cfg.Validate()
}

// applyEnv overrides config from TS_* environment variables.
func (c *Config) applyEnv() error {
	var envErr error
	override := func(env string, set func(string) error) {
		if envErr != nil {
			return
		}
		if v, ok := os.LookupEnv(env); ok {
			if err := set(v); err != nil {
				envErr = fmt.Errorf("%s: %w", env, err)
			}
		}
	}
	override("TS_LISTEN", func(v string) error { c.Listen = v; return nil })
	override("TS_DATA_DIR", func(v string) error { c.DataDir = v; return nil })
	override("TS_WAIT_WINDOW", func(v string) error {
		d, err := time.ParseDuration(v)
		if err != nil {
			return err
		}
		c.WaitWindow, c.WaitWindowString = d, v
		return nil
	})
	override("TS_MAX_TTL", func(v string) error {
		d, err := time.ParseDuration(v)
		if err != nil {
			return err
		}
		c.MaxTTL, c.MaxTTLString = d, v
		return nil
	})
	override("TS_LATENCY_THRESHOLD_MS", func(v string) error {
		n, err := strconv.ParseInt(v, 10, 64)
		if err != nil {
			return err
		}
		c.LatencyThresholdMs = n
		return nil
	})
	override("TS_PROBABILISTIC_RATE", func(v string) error {
		f, err := strconv.ParseFloat(v, 64)
		if err != nil {
			return err
		}
		c.ProbabilisticRate = f
		return nil
	})
	override("TS_BUDGET_CAPACITY", func(v string) error {
		f, err := strconv.ParseFloat(v, 64)
		if err != nil {
			return err
		}
		c.BudgetCapacity = f
		return nil
	})
	override("TS_BUDGET_REFILL_PER_SEC", func(v string) error {
		f, err := strconv.ParseFloat(v, 64)
		if err != nil {
			return err
		}
		c.BudgetRefillPerSec = f
		return nil
	})
	if envErr != nil {
		return envErr
	}
	return c.Validate()
}
