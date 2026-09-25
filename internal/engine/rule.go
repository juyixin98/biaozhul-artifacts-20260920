// Rule configuration, runtime state, samples and events.
package engine

import (
	"errors"
	"fmt"
	"time"
)

// Rule is the static configuration of a threshold alert for one metric.
//
// Hysteresis has two independent mechanisms:
//
//  1. Duration: a hot (breaching) streak must last TriggerFor before firing;
//     a cold (non-hot and non-warm) streak must last RecoverFor before
//     resolving. Durations are measured on the virtual clock using the
//     timestamps of DISTINCT samples, so repeated identical samples never
//     accumulate streak time.
//  2. Value band (optional): when RecoveryThreshold is set, values between
//     the threshold and the recovery threshold form the "warm" zone. A warm
//     sample keeps an open alert open (recovering streak is reset) without
//     re-firing; only values fully back to normal (cold) count toward
//     recovery. For upward operators (>, >=) it must be < threshold; for
//     downward operators (<, <=) it must be > threshold.
type Rule struct {
	ID         string   `json:"id"`
	Metric     string   `json:"metric"`
	Operator   Operator `json:"operator"`
	Threshold  float64  `json:"threshold"`
	TriggerFor Duration `json:"trigger_for"`
	RecoverFor Duration `json:"recover_for"`
	NoDataFor  Duration `json:"no_data_for"`
	// RecoveryThreshold is 0 by default, which disables the warm band:
	// every non-hot sample counts as cold.
	RecoveryThreshold float64 `json:"recovery_threshold,omitempty"`
	// HasRecovery distinguishes "unset" from an explicit zero.
	HasRecovery bool `json:"has_recovery_threshold,omitempty"`

	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
	// Version starts at 1 and is bumped on every configuration replacement
	// (PUT). Runtime state is reset on version change.
	Version int `json:"version"`
}

// Validate checks the configuration. defaultsFilled says whether
// applyDefaults has run (used by Restore, where defaults already exist).
func (r *Rule) Validate() error {
	if r.ID == "" {
		return errors.New("rule id is required")
	}
	if r.Metric == "" {
		return errors.New("metric is required")
	}
	if !validOp(r.Operator) {
		return fmt.Errorf("operator must be one of >, >=, <, <=, got %q", r.Operator)
	}
	if r.TriggerFor.Duration < 0 {
		return errors.New("trigger_for must not be negative")
	}
	if r.RecoverFor.Duration < 0 {
		return errors.New("recover_for must not be negative")
	}
	if r.NoDataFor.Duration <= 0 {
		return errors.New("no_data_for must be greater than zero")
	}
	if r.HasRecovery {
		switch {
		case r.Operator.upward() && !(r.RecoveryThreshold < r.Threshold):
			return fmt.Errorf("for operator %s recovery_threshold must be strictly below threshold (%v)", r.Operator, r.Threshold)
		case !r.Operator.upward() && !(r.RecoveryThreshold > r.Threshold):
			return fmt.Errorf("for operator %s recovery_threshold must be strictly above threshold (%v)", r.Operator, r.Threshold)
		}
	}
	return nil
}

// applyDefaults fills the no-data duration when unset. TriggerFor and
// RecoverFor are deliberately left at zero, which means "immediate" (fire
// on the first hot sample / resolve on the first cold sample) — matching
// the common monitoring semantics where an empty duration is an instant
// threshold.
func (r *Rule) applyDefaults(now time.Time) {
	if r.NoDataFor.Duration == 0 {
		r.NoDataFor.Duration = 2 * time.Minute
	}
	if r.CreatedAt.IsZero() {
		r.CreatedAt = now
	}
	if r.UpdatedAt.IsZero() {
		r.UpdatedAt = now
	}
	if r.Version == 0 {
		r.Version = 1
	}
}

// Sample is one metric observation at a virtual timestamp.
type Sample struct {
	Metric string    `json:"metric"`
	TS     time.Time `json:"ts"`
	Value  float64   `json:"value"`
}

// tier classifies a sample value against a rule.
type tier string

const (
	tierHot  tier = "hot"  // breaches the alert threshold
	tierWarm tier = "warm" // inside the hysteresis band (only with RecoveryThreshold)
	tierCold tier = "cold" // fully back to normal
)

// classify maps a value to hot/warm/cold against the rule.
func (r *Rule) classify(value float64) tier {
	if r.Operator.hot(value, r.Threshold) {
		return tierHot
	}
	if r.HasRecovery {
		if r.Operator.upward() {
			// threshold >= value > recovery threshold: still in the band
			if value > r.RecoveryThreshold {
				return tierWarm
			}
		} else {
			// threshold <= value < recovery threshold
			if value < r.RecoveryThreshold {
				return tierWarm
			}
		}
	}
	return tierCold
}

// Event is a state-machine transition record. Not every transition is a
// notification (see EventType.IsNotification); pending entry, for example,
// creates no event at all.
type Event struct {
	Seq     int64     `json:"seq"`
	RuleID  string    `json:"rule_id"`
	Metric  string    `json:"metric"`
	Type    EventType `json:"type"`
	TS      time.Time `json:"ts"` // virtual clock time of the transition
	From    State     `json:"from"`
	To      State     `json:"to"`
	Message string    `json:"message"`
	Value   *float64  `json:"value,omitempty"`
	Version int       `json:"version"` // rule configuration version at transition time
}

// RuntimeState is the mutable per-rule evaluation state.
type RuntimeState struct {
	State State `json:"state"`

	// HotSince / ColdSince anchor the current streak. A streak is measured
	// as clock-time elapsed since the anchor, so duplicate samples (same
	// timestamp) cannot extend it.
	HotSince  *time.Time `json:"hot_since,omitempty"`
	ColdSince *time.Time `json:"cold_since,omitempty"`

	// LastSampleTS is the timestamp of the newest sample accepted for
	// evaluation. Staleness is measured from it.
	LastSampleTS *time.Time `json:"last_sample_ts,omitempty"`
	LastValue    *float64   `json:"last_value,omitempty"`

	EnteredAt time.Time `json:"entered_at"` // when the current state was entered
	UpdatedAt time.Time `json:"updated_at"` // last state change
}

// RuleSnapshot is a value copy of a rule and its runtime state, safe for
// callers to retain.
type RuleSnapshot struct {
	Rule  Rule
	State RuntimeState
}
