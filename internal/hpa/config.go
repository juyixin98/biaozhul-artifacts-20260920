package hpa

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"time"
)

// Validation errors.
var (
	ErrEmptyScalerID         = errors.New("scalerId must not be empty")
	ErrBadTarget             = errors.New("targetUtilization must be in (0,100]")
	ErrBadTolerance          = errors.New("tolerancePct must be in [0,100)")
	ErrBadReplicaRange       = errors.New("require 0 < minReplicas <= maxReplicas")
	ErrBadWindow             = errors.New("stabilization windows must be >= 0")
	ErrBadFreshness          = errors.New("metricFreshnessSeconds must be > 0")
	ErrNegativeReplicas      = errors.New("currentReplicas must be >= 0")
	ErrInstanceCountMismatch = errors.New("number of instances must equal currentReplicas")
	ErrConfigVersionMismatch = errors.New("request configVersion does not match the active config version")
	ErrZeroMetricTimestamp   = errors.New("metricTimestamp is required")
	ErrZeroTarget            = errors.New("targetUtilization is zero (division by zero refused; update the config)")
)

// Validate checks a config and normalizes zero-valued fields to defaults.
func Validate(c *Config) error {
	if c.ScalerID == "" {
		return ErrEmptyScalerID
	}
	if c.TargetUtilization <= 0 || c.TargetUtilization > 100 {
		return ErrBadTarget
	}
	if c.TolerancePct < 0 || c.TolerancePct >= 100 {
		return ErrBadTolerance
	}
	if c.MinReplicas <= 0 || c.MaxReplicas < c.MinReplicas {
		return ErrBadReplicaRange
	}
	if c.ScaleDownStabilizationWindowSeconds < 0 ||
		c.ScaleUpStabilizationWindowSeconds < 0 {
		return ErrBadWindow
	}
	if c.MetricFreshnessSeconds <= 0 {
		return ErrBadFreshness
	}
	return nil
}

// WithDefaults fills unset/zero optional fields with HPA defaults.
func WithDefaults(c Config) Config {
	if c.TargetUtilization == 0 {
		c.TargetUtilization = DefaultTargetUtilization
	}
	if c.TolerancePct == 0 {
		c.TolerancePct = DefaultTolerancePct
	}
	if c.MinReplicas == 0 {
		c.MinReplicas = DefaultMinReplicas
	}
	if c.MaxReplicas == 0 {
		c.MaxReplicas = DefaultMaxReplicas
	}
	if c.ScaleDownStabilizationWindowSeconds == 0 {
		c.ScaleDownStabilizationWindowSeconds = DefaultScaleDownWindowSec
	}
	// ScaleUpStabilizationWindowSeconds zero is the meaningful default (0).
	if c.MetricFreshnessSeconds == 0 {
		c.MetricFreshnessSeconds = DefaultFreshnessSec
	}
	return c
}

// FingerprintConfig returns a stable SHA-256 hex digest of the policy
// *content* (metadata such as version/createdAt excluded), so two versions
// with identical parameters produce identical fingerprints.
func FingerprintConfig(c Config) string {
	canonical := canonicalConfig{
		TargetUtilization:                   c.TargetUtilization,
		TolerancePct:                        c.TolerancePct,
		MinReplicas:                         c.MinReplicas,
		MaxReplicas:                         c.MaxReplicas,
		ScaleDownStabilizationWindowSeconds: c.ScaleDownStabilizationWindowSeconds,
		ScaleUpStabilizationWindowSeconds:   c.ScaleUpStabilizationWindowSeconds,
		MetricFreshnessSeconds:              c.MetricFreshnessSeconds,
	}
	b, _ := json.Marshal(canonical) // deterministic: fixed struct, no maps
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// canonicalConfig carries exactly the policy parameters that define behavior.
type canonicalConfig struct {
	TargetUtilization                   int     `json:"targetUtilization"`
	TolerancePct                        float64 `json:"tolerancePct"`
	MinReplicas                         int     `json:"minReplicas"`
	MaxReplicas                         int     `json:"maxReplicas"`
	ScaleDownStabilizationWindowSeconds int     `json:"scaleDownStabilizationWindowSeconds"`
	ScaleUpStabilizationWindowSeconds   int     `json:"scaleUpStabilizationWindowSeconds"`
	MetricFreshnessSeconds              int     `json:"metricFreshnessSeconds"`
}

// SignDecision computes HMAC-SHA256 over a canonical serialization of the
// decision. It authenticates that the output really came from this server
// with the configured signing key.
func SignDecision(key []byte, d *Decision) string {
	mac := hmac.New(sha256.New, key)
	fmt.Fprintf(mac, "scalerId=%s\n", d.ScalerID)
	fmt.Fprintf(mac, "configVersion=%d\n", d.ConfigVersion)
	fmt.Fprintf(mac, "configFingerprint=%s\n", d.ConfigFingerprint)
	fmt.Fprintf(mac, "metricTimestamp=%s\n", d.MetricTimestamp.UTC().Format(time.RFC3339Nano))
	fmt.Fprintf(mac, "decidedAt=%s\n", d.DecidedAt.UTC().Format(time.RFC3339Nano))
	fmt.Fprintf(mac, "currentReplicas=%d\n", d.CurrentReplicas)
	fmt.Fprintf(mac, "effectiveUtilizationPct=%s\n", strconv.FormatFloat(d.EffectiveUtilPct, 'f', 4, 64))
	fmt.Fprintf(mac, "rawDesired=%d\n", d.RawDesired)
	fmt.Fprintf(mac, "rawAction=%s\n", d.RawAction)
	fmt.Fprintf(mac, "windowedDesired=%d\n", d.WindowedDesired)
	fmt.Fprintf(mac, "finalDesired=%d\n", d.FinalDesired)
	fmt.Fprintf(mac, "finalAction=%s\n", d.FinalAction)
	fmt.Fprintf(mac, "recorded=%t\n", d.RecommendationRecorded)
	return hex.EncodeToString(mac.Sum(nil))
}
