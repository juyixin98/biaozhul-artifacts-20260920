// Package models holds the domain types shared across store, evaluator and API.
package models

import "time"

// Stage is one rollout percentage. Order matters.
type Stage string

const (
	Stage5   Stage = "5%"
	Stage20  Stage = "20%"
	Stage50  Stage = "50%"
	Stage100 Stage = "100%"
)

// Stages is the immutable progression: 5% -> 20% -> 50% -> 100%.
var Stages = []Stage{Stage5, Stage20, Stage50, Stage100}

// StageIndex returns the 0-based index of s in Stages, or -1.
func StageIndex(s Stage) int {
	for i, st := range Stages {
		if st == s {
			return i
		}
	}
	return -1
}

// Command is an operator action on a release.
type Command string

const (
	CmdAdvance  Command = "advance"  // promote to the next stage, if the verdict allows
	CmdPause    Command = "pause"    // freeze traffic at the current stage
	CmdResume   Command = "resume"   // leave the paused state, still at the same stage
	CmdRollback Command = "rollback" // retreat to the previous stage (or 0% at the first)
)

// State is the release lifecycle state.
type State string

const (
	StateActive   State = "active" // serving the current stage
	StatePaused   State = "paused" // frozen by an operator; advances refused
	StateRolled   State = "rolled_back"
	StateComplete State = "complete" // reached 100% and passed
)

// Reason codes carried by every "unknown"/"degraded" or rejected decision.
type Reason string

const (
	ReasonOK                  Reason = "healthy"
	ReasonMetricsMissing      Reason = "metrics_missing"      // no data at all for the window
	ReasonWindowIncomplete    Reason = "window_incomplete"    // the full observation window has not elapsed yet
	ReasonCoverageIncomplete  Reason = "coverage_incomplete"  // buckets do not cover the whole window
	ReasonSamplesInsufficient Reason = "samples_insufficient" // below the stage minimum
	ReasonErrorRateBreached   Reason = "error_rate_breached"
	ReasonLatencyBreached     Reason = "latency_breached"
)

// ThresholdSpec is the editable, versioned health policy.
type ThresholdSpec struct {
	ErrorRateUpper float64 `json:"error_rate_upper"` // maximum tolerable error rate
	LatencyP95MS   float64 `json:"latency_p95_ms"`   // kept for human display; decision uses mean upper bound
	LatencyMeanMS  float64 `json:"latency_mean_ms"`  // upper bound of the latency mean must stay <= this
}

// DefaultThresholdSpec is the v1 policy a fresh database starts with.
func DefaultThresholdSpec() ThresholdSpec {
	return ThresholdSpec{
		ErrorRateUpper: 0.05, // 5%
		LatencyP95MS:   200,
		LatencyMeanMS:  120,
	}
}

// ThresholdVersion is one frozen policy revision.
type ThresholdVersion struct {
	Version     int           `json:"version"`
	Spec        ThresholdSpec `json:"spec"`
	CreatedAt   time.Time     `json:"created_at"`
	Description string        `json:"description"`
}

// Release is one progressive rollout.
type Release struct {
	ID               string        `json:"id"`
	Name             string        `json:"name"`
	Version          string        `json:"version"`
	State            State         `json:"state"`
	Stage            Stage         `json:"stage"`
	StageWeight      float64       `json:"stage_weight"` // 0.05, 0.20, ...
	Generation       int64         `json:"generation"`   // bumps on every applied command
	ObservationMS    int64         `json:"observation_ms"`
	MinSamples       int64         `json:"min_samples"`
	ThresholdVersion int           `json:"threshold_version"` // frozen at start
	ThresholdSpec    ThresholdSpec `json:"threshold_snapshot"`
	MetricURL        string        `json:"metric_url"`
	Scenario         string        `json:"scenario"`
	StageEnteredAt   time.Time     `json:"stage_entered_at"`
	CreatedAt        time.Time     `json:"created_at"`
	UpdatedAt        time.Time     `json:"updated_at"`
}

// MetricResult summarizes one pulled observation with real intervals.
type MetricResult struct {
	Errors          int64    `json:"errors"`
	Samples         int64    `json:"samples"`
	ErrorRatePoint  float64  `json:"error_rate_point"`
	ErrorRateLower  *float64 `json:"error_rate_lower"`
	ErrorRateUpper  *float64 `json:"error_rate_upper"`
	LatencyMeanMS   *float64 `json:"latency_mean_ms"`
	LatencyUpperMS  *float64 `json:"latency_upper_ms"`
	LatencyLowerMS  *float64 `json:"latency_lower_ms"`
	LatencyStddevMS *float64 `json:"latency_stddev_ms"`
	WindowStart     string   `json:"window_start"` // RFC3339 of the stage window used
	WindowEnd       string   `json:"window_end"`
	BucketsCovered  int      `json:"buckets_covered"`
	BucketsRequired int      `json:"buckets_required"`
}

// Verdict is the evaluator output attached to every observation and command.
type Verdict struct {
	Health      string       `json:"health"` // healthy | degraded | unknown
	Reasons     []Reason     `json:"reasons"`
	Allowed     []Command    `json:"allowed_commands"`
	ThresholdV  int          `json:"threshold_version"`
	Metrics     MetricResult `json:"metrics"`
	EvaluatedAt time.Time    `json:"evaluated_at"`
	// Late is true when this verdict was computed for a stage the release has
	// since left — such verdicts are evidence only and never drive a command.
	Late bool `json:"late,omitempty"`
}

// Event is an append-only audit record.
type Event struct {
	Seq        int64     `json:"seq"`
	ReleaseID  string    `json:"release_id"`
	Generation int64     `json:"generation"`
	Type       string    `json:"type"` // release_created | advanced | paused | resumed | rolled_back | rejected | observed
	Command    Command   `json:"command,omitempty"`
	FromStage  Stage     `json:"from_stage,omitempty"`
	ToStage    Stage     `json:"to_stage,omitempty"`
	Verdict    *Verdict  `json:"verdict,omitempty"`
	Detail     string    `json:"detail,omitempty"`
	CreatedAt  time.Time `json:"created_at"`
}
