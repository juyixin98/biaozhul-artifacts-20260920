package budget

import (
	"encoding/json"
	"errors"
	"fmt"

	"tokenbudget/internal/rational"
)

// Validation bounds. Generous for any real rate-limit use while keeping all
// internal 128-bit intermediates safely bounded (rate ~1e9 tok/s, dt ~1e18 ns,
// scale 1e6 => ~1e33 < 2^128 ~ 3.4e38).
const (
	MaxRateTokensPerSecond int64 = 1_000_000_000
	MaxBurstTokens         int64 = 1_000_000_000
)

// BucketConfig is the exact configuration of one bucket layer.
type BucketConfig struct {
	// Rate is the refill rate in tokens per second (exact fraction).
	Rate rational.Rate `json:"rate"`
	// BurstMicro is the capacity in microtokens (1 token = 1e6).
	BurstMicro int64 `json:"burst_micro"`
}

// CapacityMicro returns the capacity (alias kept for readability).
func (c BucketConfig) CapacityMicro() int64 { return c.BurstMicro }

func (c BucketConfig) validate() error {
	if c.Rate.Num < 0 || c.Rate.Den <= 0 {
		return errors.New("budget: rate must be non-negative with positive denominator")
	}
	if c.Rate.Num > MaxRateTokensPerSecond*c.Rate.Den {
		return fmt.Errorf("budget: rate %s exceeds maximum %d tokens/sec", c.Rate, MaxRateTokensPerSecond)
	}
	// Internal microtoken refill divides by (Den*1000); keep that divisor
	// inside uint64 (also comfortably below any realistic slow rate).
	if c.Rate.Den > 1_000_000_000_000 {
		return errors.New("budget: rate denominator too large (slowest supported rate is 1 token / 1e12 s)")
	}
	if c.BurstMicro < 0 {
		return errors.New("budget: burst must be non-negative")
	}
	if c.BurstMicro > MaxBurstTokens*MicroPerToken {
		return fmt.Errorf("budget: burst exceeds maximum %d tokens", MaxBurstTokens)
	}
	return nil
}

// Config is the whole limiter configuration: a global layer plus either a
// default applied to every tenant, or per-tenant overrides (a tenant with an
// override uses it; everyone else uses Default).
type Config struct {
	Global  BucketConfig            `json:"global"`
	Default BucketConfig            `json:"default"`
	Tenants map[string]BucketConfig `json:"tenants,omitempty"`
}

// ConfigView is the JSON-serializable view, adding burst as an exact decimal
// token string for human readers.
type ConfigView struct {
	Global  BucketConfigView            `json:"global"`
	Default BucketConfigView            `json:"default"`
	Tenants map[string]BucketConfigView `json:"tenants,omitempty"`
}

// BucketConfigView renders config with both raw microtoken and exact decimal
// forms.
type BucketConfigView struct {
	Rate       rational.Rate `json:"rate"`
	Burst      string        `json:"burst"`
	BurstMicro int64         `json:"burst_micro"`
}

func view(c BucketConfig) BucketConfigView {
	return BucketConfigView{
		Rate:       c.Rate,
		Burst:      rational.FormatMicroTokens(c.BurstMicro),
		BurstMicro: c.BurstMicro,
	}
}

// View returns a JSON-friendly copy of the config.
func (c Config) View() *ConfigView {
	v := &ConfigView{Global: view(c.Global), Default: view(c.Default)}
	if len(c.Tenants) > 0 {
		v.Tenants = make(map[string]BucketConfigView, len(c.Tenants))
		for t, cc := range c.Tenants {
			v.Tenants[t] = view(cc)
		}
	}
	return v
}

// Validate checks every layer.
func (c Config) Validate() error {
	if err := c.Global.validate(); err != nil {
		return fmt.Errorf("global: %w", err)
	}
	if err := c.Default.validate(); err != nil {
		return fmt.Errorf("default: %w", err)
	}
	for t, cc := range c.Tenants {
		if err := cc.validate(); err != nil {
			return fmt.Errorf("tenant %q: %w", t, err)
		}
	}
	return nil
}

// MarshalJSON renders config through its exact view.
func (c Config) MarshalJSON() ([]byte, error) {
	return json.Marshal(c.View())
}
