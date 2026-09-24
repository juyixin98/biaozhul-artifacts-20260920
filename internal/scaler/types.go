package scaler

import (
	"errors"
	"time"
)

// Action is the outcome of one evaluation.
type Action string

const (
	ActionScaleUp   Action = "scale-up"
	ActionScaleDown Action = "scale-down"
	ActionHold      Action = "hold" // evaluated, decided not to change
	ActionSkip      Action = "skip" // could not evaluate (no usable metrics)
)

// Config tunes the controller. Durations are in seconds so the offline
// controller can be driven with client-supplied timestamps.
type Config struct {
	MinReplicas      int     `json:"min_replicas"`
	MaxReplicas      int     `json:"max_replicas"`
	TargetPerReplica float64 `json:"target_per_replica"` // target load per replica, same unit as Sample.Value
	Tolerance        float64 `json:"tolerance"`          // hysteresis band on the usage ratio, e.g. 0.1 = ±10%

	MetricsWindowSeconds int `json:"metrics_window_seconds"` // samples older than this are stale
	ColdStartSeconds     int `json:"cold_start_seconds"`     // pods younger than this are excluded

	ScaleUpStabilizationSeconds   int `json:"scale_up_stabilization_seconds"`
	ScaleDownStabilizationSeconds int `json:"scale_down_stabilization_seconds"`

	ScaleUpCooldownSeconds   int `json:"scale_up_cooldown_seconds"`
	ScaleDownCooldownSeconds int `json:"scale_down_cooldown_seconds"`

	ScaleUpMaxStep   int `json:"scale_up_max_step"`   // rate limit: max replicas added per action
	ScaleDownMaxStep int `json:"scale_down_max_step"` // rate limit: max replicas removed per action
}

// DefaultConfig returns a reasonable starting configuration.
func DefaultConfig() Config {
	return Config{
		MinReplicas:      1,
		MaxReplicas:      10,
		TargetPerReplica: 50,
		Tolerance:        0.1,

		MetricsWindowSeconds: 60,
		ColdStartSeconds:     30,

		ScaleUpStabilizationSeconds:   0,
		ScaleDownStabilizationSeconds: 300,

		ScaleUpCooldownSeconds:   60,
		ScaleDownCooldownSeconds: 120,

		ScaleUpMaxStep:   2,
		ScaleDownMaxStep: 1,
	}
}

// Validate checks the config for consistency.
func (c Config) Validate() error {
	if c.MinReplicas < 1 {
		return errors.New("min_replicas must be >= 1")
	}
	if c.MaxReplicas < c.MinReplicas {
		return errors.New("max_replicas must be >= min_replicas")
	}
	if c.TargetPerReplica <= 0 {
		return errors.New("target_per_replica must be > 0")
	}
	if c.Tolerance < 0 || c.Tolerance >= 1 {
		return errors.New("tolerance must be in [0, 1)")
	}
	if c.MetricsWindowSeconds < 0 || c.ColdStartSeconds < 0 ||
		c.ScaleUpStabilizationSeconds < 0 || c.ScaleDownStabilizationSeconds < 0 ||
		c.ScaleUpCooldownSeconds < 0 || c.ScaleDownCooldownSeconds < 0 {
		return errors.New("durations must be >= 0")
	}
	if c.ScaleUpMaxStep < 1 || c.ScaleDownMaxStep < 1 {
		return errors.New("max steps must be >= 1")
	}
	return nil
}

// Sample is one metric data point for one pod.
type Sample struct {
	Time     time.Time `json:"time"`      // when the metric was measured
	Pod      string    `json:"pod"`       // pod identifier
	PodStart time.Time `json:"pod_start"` // when the pod started (for cold-start detection)
	Value    float64   `json:"value"`     // load value, same unit as TargetPerReplica
}

// PodInfo records a sample that was used in a decision.
type PodInfo struct {
	Pod        string    `json:"pod"`
	Value      float64   `json:"value"`
	SampleTime time.Time `json:"sample_time"`
}

// ExcludedPod records a pod that was left out of a decision, with the reason.
type ExcludedPod struct {
	Pod    string `json:"pod"`
	Reason string `json:"reason"`
}

// Decision is the full audit record of one evaluation: which samples were
// used, which were excluded and why, and every reason that shaped the result.
type Decision struct {
	Seq               int           `json:"seq"`
	Time              time.Time     `json:"time"`
	Action            Action        `json:"action"`
	CurrentReplicas   int           `json:"current_replicas"`
	DesiredReplicas   int           `json:"desired_replicas"`
	UsageRatio        float64       `json:"usage_ratio"` // avg load per reporting pod / target; 0 when skipped
	RawDesired        int           `json:"raw_desired"`
	StabilizedDesired int           `json:"stabilized_desired"`
	SamplesUsed       []PodInfo     `json:"samples_used"`
	Excluded          []ExcludedPod `json:"excluded"`
	Reasons           []string      `json:"reasons"`
}

// State is a point-in-time snapshot of the controller.
type State struct {
	Replicas      int        `json:"replicas"`
	Config        Config     `json:"config"`
	LastScaleUp   *time.Time `json:"last_scale_up,omitempty"`
	LastScaleDown *time.Time `json:"last_scale_down,omitempty"`
	DecisionCount int        `json:"decision_count"`
}
