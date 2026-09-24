package config

import (
	"errors"
	"time"

	"sensorhealth/internal/domain"
)

// DefaultRuleConfig returns a sane baseline; individual device-type configs in
// the database override these values.
func DefaultRuleConfig(deviceType string, version int64) domain.RuleConfig {
	c := domain.RuleConfig{
		DeviceType:             deviceType,
		Version:                version,
		StaleEnterTimeout:      domain.Duration(90 * time.Second),
		StaleRecoverTimeout:    domain.Duration(30 * time.Second),
		FrozenEnterCount:       6,
		FrozenEnterMinDuration: domain.Duration(30 * time.Second),
		FrozenRecoverCount:     2,
		MissingEnterCount:      3,
		MissingRecoverCount:    1,
		BackfillLookback:       1000,
	}
	return c
}

// Validate checks a rule config for internal consistency. Recovery thresholds
// MUST be stricter (fewer samples / shorter time) than entry thresholds so the
// system has hysteresis: a flapping borderline input cannot instantly toggle
// the alert.
func Validate(c domain.RuleConfig) error {
	if c.DeviceType == "" {
		return errors.New("device_type is required")
	}
	if c.StaleEnterTimeout.Std() <= 0 || c.StaleRecoverTimeout.Std() <= 0 {
		return errors.New("stale timeouts must be positive")
	}
	if c.StaleRecoverTimeout.Std() >= c.StaleEnterTimeout.Std() {
		return errors.New("stale_recover_timeout must be shorter than stale_enter_timeout (hysteresis)")
	}
	if c.FrozenEnterCount <= 0 || c.FrozenRecoverCount <= 0 {
		return errors.New("frozen counts must be positive")
	}
	if c.FrozenRecoverCount >= c.FrozenEnterCount {
		return errors.New("frozen_recover_count must be smaller than frozen_enter_count (hysteresis)")
	}
	if c.FrozenEnterMinDuration.Std() < 0 {
		return errors.New("frozen_enter_min_duration must be >= 0")
	}
	if c.MissingEnterCount <= 0 || c.MissingRecoverCount <= 0 {
		return errors.New("missing counts must be positive")
	}
	if c.MissingRecoverCount >= c.MissingEnterCount {
		return errors.New("missing_recover_count must be smaller than missing_enter_count (hysteresis)")
	}
	if c.BackfillLookback < 0 {
		return errors.New("backfill_lookback must be >= 0")
	}
	return nil
}
