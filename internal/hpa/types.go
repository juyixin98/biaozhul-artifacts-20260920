// Package hpa implements a Kubernetes-HPA-style local scaling decision
// engine: target-utilization replica formula, a tolerance band for flapping,
// and scale-down stabilization windows whose decision is the maximum
// recommendation observed inside the window.
package hpa

import (
	"time"
)

// Config is the autoscaling policy for one scaler.
//
//	RawDesired = ceil(currentReplicas * currentUtilization / TargetUtilization)
//
// Tolerance is the symmetric tolerance band around the target: while
// utilization remains inside it the desired replica count is held.
// ScaleDownStabilizationWindowSeconds is the classic HPA stable window:
// during a scale-down the engine picks the MAXIMUM raw recommendation seen
// inside the window, so a single early, conservative recommendation keeps
// the fleet large until the window slides past it. ScaleUpStabilizationWindow
// applies the same rule to scale-ups (0 = act immediately, HPA default).
type Config struct {
	ScalerID                            string    `json:"scalerId"`
	Version                             int64     `json:"version"`
	TargetUtilization                   int       `json:"targetUtilization"`
	TolerancePct                        float64   `json:"tolerancePct"`
	MinReplicas                         int       `json:"minReplicas"`
	MaxReplicas                         int       `json:"maxReplicas"`
	ScaleDownStabilizationWindowSeconds int       `json:"scaleDownStabilizationWindowSeconds"`
	ScaleUpStabilizationWindowSeconds   int       `json:"scaleUpStabilizationWindowSeconds"`
	MetricFreshnessSeconds              int       `json:"metricFreshnessSeconds"`
	CreatedAt                           time.Time `json:"createdAt"`
	Fingerprint                         string    `json:"fingerprint"`
}

// InstanceReport is one pod's sample for one decision tick.
// UtilizationPct == nil means "metric missing for this instance" — it must
// never be silently replaced with 0.
type InstanceReport struct {
	Name           string   `json:"name"`
	Ready          bool     `json:"ready"`
	UtilizationPct *float64 `json:"utilizationPct"`
}

// InstanceDetail shows how one pod was accounted for in the formula.
type InstanceDetail struct {
	Name             string   `json:"name"`
	Ready            bool     `json:"ready"`
	ReportedPct      *float64 `json:"reportedPct,omitempty"`
	EffectivePct     float64  `json:"effectivePct"`
	Missing          bool     `json:"missing"`
	ExcludedUnready  bool     `json:"excludedUnready"`
	AssumedForSafety bool     `json:"assumedForSafety"`
	Note             string   `json:"note"`
}

// DecisionRequest binds one evaluation to a configuration version and to the
// time at which the metrics were observed.
type DecisionRequest struct {
	ScalerID        string           `json:"scalerId"`
	ConfigVersion   int64            `json:"configVersion"`
	CurrentReplicas int              `json:"currentReplicas"`
	Instances       []InstanceReport `json:"instances"`
	// MetricTimestamp is event time: when the metrics were actually scraped.
	// The stable window is measured in event time, never wall-clock time.
	MetricTimestamp time.Time `json:"metricTimestamp"`
}

// Analysis is the pure arithmetic result, before the stable window is applied.
type Analysis struct {
	ReadyTotal       int              `json:"readyTotal"`
	UnreadyTotal     int              `json:"unreadyTotal"`
	ReadyReporting   int              `json:"readyReporting"`
	ReadyMissing     int              `json:"readyMissing"`
	EffectiveUtilPct float64          `json:"effectiveUtilizationPct"`
	ReportedUtilPct  *float64         `json:"reportedUtilizationPct,omitempty"`
	WithinTolerance  bool             `json:"withinTolerance"`
	Ratio            float64          `json:"ratioVsTarget"`
	InstanceDetails  []InstanceDetail `json:"instanceDetails"`

	// RawDesired is the formula output rounded up and bounded by min/max.
	RawDesired   int    `json:"rawDesired"`
	RawAction    Action `json:"rawAction"`
	RawReason    string `json:"rawReason"`
	RawCappedMin bool   `json:"rawCappedAtMin"`
	RawCappedMax bool   `json:"rawCappedAtMax"`

	// Hold fields are set when no formula evaluation is possible.
	Hold       bool   `json:"-"`
	HoldReason string `json:"-"`
}

// Action is ScaleUp / ScaleDown / Hold.
type Action string

const (
	ScaleUp   Action = "ScaleUp"
	ScaleDown Action = "ScaleDown"
	Hold      Action = "Hold"
)

// Decision is the final result returned to clients and persisted.
type Decision struct {
	ScalerID          string    `json:"scalerId"`
	ConfigVersion     int64     `json:"configVersion"`
	ConfigFingerprint string    `json:"configFingerprint"`
	MetricTimestamp   time.Time `json:"metricTimestamp"`
	DecidedAt         time.Time `json:"decidedAt"`
	CurrentReplicas   int       `json:"currentReplicas"`

	ReadyTotal       int      `json:"readyTotal"`
	UnreadyTotal     int      `json:"unreadyTotal"`
	ReadyReporting   int      `json:"readyReporting"`
	ReadyMissing     int      `json:"readyMissing"`
	EffectiveUtilPct float64  `json:"effectiveUtilizationPct"`
	ReportedUtilPct  *float64 `json:"reportedUtilizationPct,omitempty"`
	WithinTolerance  bool     `json:"withinTolerance"`

	// RawDesired is the HPA formula recommendation.
	RawDesired int    `json:"rawDesired"`
	RawAction  Action `json:"rawAction"`
	RawReason  string `json:"rawReason"`

	// WindowedDesired is the stable-window recommendation.
	WindowedDesired int `json:"windowedDesired"`
	WindowSeconds   int `json:"windowSecondsApplied"`
	WindowSamples   int `json:"windowSamplesConsidered"`

	// FinalDesired is the recommendation the caller should act on.
	FinalDesired   int      `json:"finalDesired"`
	FinalAction    Action   `json:"finalAction"`
	FinalReasons   []string `json:"reasons"`
	FinalCappedMin bool     `json:"finalCappedAtMin"`
	FinalCappedMax bool     `json:"finalCappedAtMax"`

	// RecommendationRecorded is false when data was insufficient and no
	// formula output was added to the stable window.
	RecommendationRecorded bool `json:"recommendationRecorded"`

	InstanceDetails []InstanceDetail `json:"instanceDetails"`
	Signature       string           `json:"signature"`
}
