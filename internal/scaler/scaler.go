// Package scaler contains the pure autoscaling decision math: the HPA target
// utilization formula, the tolerance band and the scale-down stable window.
//
// It has no I/O and no wall-clock access, which keeps the decision rules
// unit-testable with a fully controlled clock.
package scaler

import (
	"errors"
	"math"
	"sort"
)

// Sentinel errors. A zero target utilization is reported separately because it
// is a configuration-level problem (division by zero is never silently turned
// into a recommendation).
var (
	ErrZeroTarget  = errors.New("target utilization percent must be greater than zero")
	ErrInvalidConf = errors.New("invalid scaling configuration")
)

// Config is the versioned scaling configuration for one workload.
type Config struct {
	MinReplicas     int     `json:"minReplicas"`
	MaxReplicas     int     `json:"maxReplicas"`
	TargetPct       float64 `json:"targetPct"`       // desired utilization, percent, must be > 0
	TolerancePct    float64 `json:"tolerancePct"`    // symmetric dead-band around target/target ratio, percent (K8s default 10)
	StableWindowSec int     `json:"stableWindowSec"` // scale-down look-back window, seconds
}

// Validate checks the configuration. TargetPct==0 is reported as
// ErrZeroTarget so callers can distinguish it from other bad configurations.
func (c Config) Validate() error {
	if c.TargetPct == 0 {
		return ErrZeroTarget
	}
	if c.TargetPct < 0 {
		return ErrInvalidConf
	}
	if c.MinReplicas < 0 || c.MaxReplicas < 1 || c.MinReplicas > c.MaxReplicas {
		return ErrInvalidConf
	}
	if c.TolerancePct < 0 || c.TolerancePct > 100 {
		return ErrInvalidConf
	}
	if c.StableWindowSec < 0 {
		return ErrInvalidConf
	}
	return nil
}

// PodSample is one instance in a workload snapshot.
type PodSample struct {
	Name           string   `json:"name"`
	Ready          bool     `json:"ready"`
	UtilizationPct *float64 `json:"utilizationPct"` // nil == metric missing; it is NEVER filled with zero
}

// Snapshot is the workload state observed at one point in time.
type Snapshot struct {
	CurrentReplicas int // desired baseline; zero falls back to len(Pods)
	Pods            []PodSample
}

// WindowEntry is one past raw proposal considered by the scale-down stable
// window. Only raw proposals (samples outside the tolerance band) are stored,
// matching the upstream HPA controller behavior.
type WindowEntry struct {
	MetricTimeMs int64
	RawProposed  int
}

// Decision is the full result of one evaluation. RawProposed/Stabilized carry
// -1 when no numeric recommendation was produced; Final is always populated
// because a bounded, actionable recommendation must exist.
type Decision struct {
	CurrentReplicas int
	ReadyCount      int
	UnreadyCount    int
	ReadyReporting  int
	ReadyMissing    int

	AvgUtilizationPct float64 // average over READY pods that reported a metric
	MetricPresent     bool
	TargetPct         float64
	Ratio             float64 // AvgUtilizationPct / TargetPct; 0 when no metric

	RawProposed int // raw HPA formula output (ceil), -1 if hold/hold-all-missing
	Stabilized  int // after stable-window logic, -1 when the window yielded no scale-down

	Final   int      // bounded, actionable recommendation
	Action  string   // scaleup | scaledown | hold
	Reasons []string // machine-readable reason codes, in evaluation order

	// WindowUsed is the set of entries (strictly older than "now") that took
	// part in the scale-down maximum; useful for test assertions/auditing.
	WindowUsed []WindowEntry
}

// Standardized reason codes.
const (
	ReasonAllMetricsMissing = "ALL_METRICS_MISSING" // no ready pod reported a metric
	ReasonUnreadyExcluded   = "UNREADY_INSTANCES_EXCLUDED"
	ReasonPartialMetrics    = "PARTIAL_METRICS_PRESENT"
	ReasonWithinTolerance   = "WITHIN_TOLERANCE"
	ReasonRawCeil           = "RAW_ROUNDED_UP"
	ReasonScaleUpImmediate  = "SCALE_UP_IMMEDIATE"
	ReasonStableWindow      = "STABLE_WINDOW_SCALE_DOWN_MAX"
	ReasonWindowNoProposal  = "STABLE_WINDOW_NO_PROPOSAL_HOLD"
	ReasonCappedMax         = "CAPPED_AT_MAX_REPLICAS"
	ReasonCappedMin         = "CLAMPED_AT_MIN_REPLICAS"
)

const maxReasonCount = 8

// ratioTolerance converts the percent tolerance to the fractional dead-band
// applied to the utilization ratio (ratio == 1 means exactly at target).
func ratioTolerance(c Config) float64 { return c.TolerancePct / 100.0 }

// ceilWithEpsilon mirrors the HPA tolerance-aware rounding: a value within one
// scale-tolerance of an integer is treated as that integer instead of taking
// the ceiling (so 5.99999... does not become 6 due to float noise).
func ceilWithEpsilon(x, tol float64) int {
	return int(math.Ceil(x - tol))
}

// Calculate evaluates one decision. nowMs is the decision ("event") time;
// history must contain strictly older raw proposals for the same workload and
// config version.
func Calculate(c Config, snap Snapshot, nowMs int64, history []WindowEntry) (Decision, error) {
	if err := c.Validate(); err != nil {
		return Decision{}, err
	}

	d := Decision{
		TargetPct:   c.TargetPct,
		RawProposed: -1,
		Stabilized:  -1,
		Final:       0,
		WindowUsed:  []WindowEntry{},
		Reasons:     []string{},
	}

	current := snap.CurrentReplicas
	if current == 0 {
		current = len(snap.Pods)
	}
	if current < 1 {
		current = 1
	}
	d.CurrentReplicas = current

	addReason := func(r string) {
		if len(d.Reasons) < maxReasonCount {
			d.Reasons = append(d.Reasons, r)
		}
	}

	// Partition pods. Ready pods with a missing metric are counted but do NOT
	// contribute zero to the average. Unready pods never contribute a metric
	// (a cold instance's ~0% utilization must not dilute the result).
	var sum float64
	for _, p := range snap.Pods {
		if !p.Ready {
			d.UnreadyCount++
			continue
		}
		d.ReadyCount++
		if p.UtilizationPct == nil {
			d.ReadyMissing++
			continue
		}
		d.ReadyReporting++
		sum += *p.UtilizationPct
	}
	if d.UnreadyCount > 0 {
		addReason(ReasonUnreadyExcluded)
	}

	// Missing-metric policy: hold current replicas. With zero data points the
	// average is undefined — it must not be computed as zero.
	if d.ReadyReporting == 0 {
		addReason(ReasonAllMetricsMissing)
		d.Final = d.CurrentReplicas
		d.Action = "hold"
		return d, nil
	}
	if d.ReadyMissing > 0 {
		addReason(ReasonPartialMetrics)
	}

	d.MetricPresent = true
	d.AvgUtilizationPct = sum / float64(d.ReadyReporting)
	d.Ratio = d.AvgUtilizationPct / c.TargetPct
	tol := ratioTolerance(c)

	// Tolerance band: 1±tol around the target ratio. Inside it, no scale is
	// recommended and nothing is recorded in the stable window.
	if math.Abs(d.Ratio-1) <= tol {
		addReason(ReasonWithinTolerance)
		d.Final = d.CurrentReplicas
		d.Action = "hold"
		return d, nil
	}

	raw := ceilWithEpsilon(d.Ratio*float64(d.CurrentReplicas), tol)
	if raw < 1 {
		raw = 1
	}
	d.RawProposed = raw
	addReason(ReasonRawCeil)

	if raw > d.CurrentReplicas {
		// Scale-up: immediate, stable window is not consulted.
		d.Stabilized = raw
		addReason(ReasonScaleUpImmediate)
	} else {
		// Scale-down: use the MAX raw proposal inside the stable window. The
		// current raw proposal is appended AFTER reading history, so its own
		// timestamp must be strictly older than now to be eligible.
		cutoff := nowMs - int64(c.StableWindowSec)*1000
		maxProp := 0
		for _, e := range history {
			if e.MetricTimeMs < nowMs && e.MetricTimeMs >= cutoff {
				d.WindowUsed = append(d.WindowUsed, e)
				if e.RawProposed > maxProp {
					maxProp = e.RawProposed
				}
			}
		}
		if maxProp > 0 {
			d.Stabilized = maxProp
			addReason(ReasonStableWindow)
		} else {
			// Empty/short window: be conservative and hold.
			d.Stabilized = d.CurrentReplicas
			addReason(ReasonWindowNoProposal)
		}
	}

	d.Final = d.Stabilized
	if d.Final > c.MaxReplicas {
		d.Final = c.MaxReplicas
		addReason(ReasonCappedMax)
	}
	if d.Final < c.MinReplicas {
		d.Final = c.MinReplicas
		addReason(ReasonCappedMin)
	}

	switch {
	case d.Final > d.CurrentReplicas:
		d.Action = "scaleup"
	case d.Final < d.CurrentReplicas:
		d.Action = "scaledown"
	default:
		d.Action = "hold"
	}
	return d, nil
}

// SortWindowEntries orders entries by metric time ascending. Exported for
// storage layers that hand Calculate a deterministic slice.
func SortWindowEntries(e []WindowEntry) {
	sort.Slice(e, func(i, j int) bool { return e[i].MetricTimeMs < e[j].MetricTimeMs })
}
