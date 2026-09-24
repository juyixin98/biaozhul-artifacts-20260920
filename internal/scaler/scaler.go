// Package scaler implements an offline hysteresis autoscaler: it computes a
// desired replica count from time-series metrics while applying cold-start
// exclusion, stabilization windows, cooldowns, rate limits and min/max bounds.
//
// The controller is offline: it never reads a clock or talks to a cluster on
// its own. Callers ingest feed it samples (Ingest) and ask for a decision at an
// explicit point in time (Evaluate), which makes every decision deterministic
// and auditable.
package scaler

import (
	"fmt"
	"math"
	"sort"
	"sync"
	"time"
)

// recommendation is one raw desired-replica computation, kept so stabilization
// windows can look back in time.
type recommendation struct {
	at      time.Time
	desired int
}

// Scaler is the controller. It is safe for concurrent use.
type Scaler struct {
	mu       sync.Mutex
	cfg      Config
	replicas int

	latest   map[string]Sample    // pod -> most recent sample
	podStart map[string]time.Time // pod -> earliest observed start time

	recs []recommendation // recent raw recommendations, for stabilization windows

	lastUp   time.Time
	lastDown time.Time
	hasUp    bool
	hasDown  bool

	decisions []Decision
	seq       int
}

// New creates a Scaler. The initial replica count is clamped into [min, max].
func New(cfg Config, initialReplicas int) *Scaler {
	if initialReplicas < cfg.MinReplicas {
		initialReplicas = cfg.MinReplicas
	}
	if initialReplicas > cfg.MaxReplicas {
		initialReplicas = cfg.MaxReplicas
	}
	return &Scaler{
		cfg:      cfg,
		replicas: initialReplicas,
		latest:   make(map[string]Sample),
		podStart: make(map[string]time.Time),
	}
}

// Config returns the current configuration.
func (s *Scaler) Config() Config {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.cfg
}

// UpdateConfig validates and installs a new configuration. The current replica
// count is clamped into the new bounds.
func (s *Scaler) UpdateConfig(cfg Config) error {
	if err := cfg.Validate(); err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.cfg = cfg
	if s.replicas < cfg.MinReplicas {
		s.replicas = cfg.MinReplicas
	}
	if s.replicas > cfg.MaxReplicas {
		s.replicas = cfg.MaxReplicas
	}
	return nil
}

// Ingest stores samples, keeping only the newest sample per pod. A sample with
// a zero PodStart is treated as if the pod started at the sample time, i.e. it
// will be considered cold. Returns the number of samples accepted.
func (s *Scaler) Ingest(samples []Sample) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	n := 0
	for _, sm := range samples {
		if sm.Pod == "" || sm.Time.IsZero() {
			continue
		}
		if sm.PodStart.IsZero() {
			sm.PodStart = sm.Time
		}
		if cur, ok := s.latest[sm.Pod]; !ok || sm.Time.After(cur.Time) {
			s.latest[sm.Pod] = sm
		}
		if st, ok := s.podStart[sm.Pod]; !ok || sm.PodStart.Before(st) {
			s.podStart[sm.Pod] = sm.PodStart
		}
		n++
	}
	return n
}

// Evaluate computes one scaling decision as of `now` and applies it to the
// controller's internal replica count. The full decision (samples used,
// exclusions, reasons) is recorded and returned.
func (s *Scaler) Evaluate(now time.Time) Decision {
	s.mu.Lock()
	defer s.mu.Unlock()

	s.seq++
	d := Decision{
		Seq:             s.seq,
		Time:            now,
		Action:          ActionHold,
		CurrentReplicas: s.replicas,
		DesiredReplicas: s.replicas,
	}
	defer func() { s.decisions = append(s.decisions, d) }()

	// --- 1. Select usable samples -------------------------------------
	// Pods are deterministic-sorted so decisions are reproducible.
	pods := make([]string, 0, len(s.podStart))
	for p := range s.podStart {
		pods = append(pods, p)
	}
	sort.Strings(pods)

	staleBefore := now.Add(-time.Duration(s.cfg.MetricsWindowSeconds) * time.Second)
	coldBefore := time.Duration(s.cfg.ColdStartSeconds) * time.Second

	var sum float64
	for _, pod := range pods {
		sm, ok := s.latest[pod]
		if !ok || sm.Time.Before(staleBefore) {
			// Missing or stale metrics are NOT zero load: the pod is
			// excluded from the average entirely.
			d.Excluded = append(d.Excluded, ExcludedPod{
				Pod:    pod,
				Reason: "metrics missing or stale: excluded from average, not treated as zero load",
			})
			continue
		}
		if age := now.Sub(s.podStart[pod]); age < coldBefore {
			d.Excluded = append(d.Excluded, ExcludedPod{
				Pod:    pod,
				Reason: fmt.Sprintf("cold start: pod age %s < %s, metrics not yet trustworthy", age.Round(time.Second), coldBefore),
			})
			continue
		}
		d.SamplesUsed = append(d.SamplesUsed, PodInfo{Pod: pod, Value: sm.Value, SampleTime: sm.Time})
		sum += sm.Value
	}

	if len(d.SamplesUsed) == 0 {
		d.Action = ActionSkip
		d.Reasons = append(d.Reasons,
			"no usable metrics (all pods missing/stale/cold): holding current replicas; missing metrics are never treated as zero load")
		return d
	}

	// --- 2. Raw recommendation from the usage ratio --------------------
	avg := sum / float64(len(d.SamplesUsed))
	ratio := avg / s.cfg.TargetPerReplica
	d.UsageRatio = ratio

	raw := s.replicas
	if math.Abs(ratio-1) <= s.cfg.Tolerance {
		d.Reasons = append(d.Reasons, fmt.Sprintf(
			"usage ratio %.3f (avg %.1f / target %.1f over %d pods) within tolerance ±%.2f: no change",
			ratio, avg, s.cfg.TargetPerReplica, len(d.SamplesUsed), s.cfg.Tolerance))
	} else {
		raw = int(math.Ceil(float64(s.replicas) * ratio))
		d.Reasons = append(d.Reasons, fmt.Sprintf(
			"usage ratio %.3f (avg %.1f / target %.1f over %d pods) outside tolerance ±%.2f: raw desired = ceil(%d x %.3f) = %d",
			ratio, avg, s.cfg.TargetPerReplica, len(d.SamplesUsed), s.cfg.Tolerance, s.replicas, ratio, raw))
	}
	d.RawDesired = raw

	// --- 3. Stabilization window ---------------------------------------
	s.recs = append(s.recs, recommendation{at: now, desired: raw})
	maxWin := s.cfg.ScaleUpStabilizationSeconds
	if s.cfg.ScaleDownStabilizationSeconds > maxWin {
		maxWin = s.cfg.ScaleDownStabilizationSeconds
	}
	cutoff := now.Add(-time.Duration(maxWin) * time.Second)
	kept := s.recs[:0]
	for _, r := range s.recs {
		if !r.at.Before(cutoff) {
			kept = append(kept, r)
		}
	}
	s.recs = kept

	stabilized := raw
	switch {
	case raw > s.replicas:
		// Scale-up: take the max recommendation inside the (usually short)
		// upscale window.
		w := now.Add(-time.Duration(s.cfg.ScaleUpStabilizationSeconds) * time.Second)
		for _, r := range s.recs {
			if !r.at.Before(w) && r.desired > stabilized {
				stabilized = r.desired
			}
		}
		if stabilized != raw {
			d.Reasons = append(d.Reasons, fmt.Sprintf(
				"scale-up stabilization window (%ds): raised desired from %d to %d (max of recent recommendations)",
				s.cfg.ScaleUpStabilizationSeconds, raw, stabilized))
		}
	case raw < s.replicas:
		// Scale-down: take the max recommendation inside the (long) downscale
		// window — i.e. only scale down to the highest recent need. This is
		// the scale-down protection against flapping.
		w := now.Add(-time.Duration(s.cfg.ScaleDownStabilizationSeconds) * time.Second)
		for _, r := range s.recs {
			if !r.at.Before(w) && r.desired > stabilized {
				stabilized = r.desired
			}
		}
		if stabilized != raw {
			d.Reasons = append(d.Reasons, fmt.Sprintf(
				"scale-down stabilization window (%ds): raised desired from %d to %d (max of recent recommendations, protects against premature scale-down)",
				s.cfg.ScaleDownStabilizationSeconds, raw, stabilized))
		}
		// A scale-down evaluation must never turn into a scale-up: cap the
		// stabilized value at the current replica count.
		if stabilized > s.replicas {
			stabilized = s.replicas
		}
	}
	d.StabilizedDesired = stabilized

	// --- 4. Cooldown and rate limit ------------------------------------
	desired := stabilized
	if desired > s.replicas {
		if s.hasUp && now.Sub(s.lastUp) < time.Duration(s.cfg.ScaleUpCooldownSeconds)*time.Second {
			d.Reasons = append(d.Reasons, fmt.Sprintf(
				"scale-up cooldown: %s since last scale-up < %ds, staying at %d",
				now.Sub(s.lastUp).Round(time.Second), s.cfg.ScaleUpCooldownSeconds, s.replicas))
			desired = s.replicas
		} else if desired-s.replicas > s.cfg.ScaleUpMaxStep {
			d.Reasons = append(d.Reasons, fmt.Sprintf(
				"scale-up rate limit: wanted +%d, capped to +%d per action",
				desired-s.replicas, s.cfg.ScaleUpMaxStep))
			desired = s.replicas + s.cfg.ScaleUpMaxStep
		}
	} else if desired < s.replicas {
		if s.hasDown && now.Sub(s.lastDown) < time.Duration(s.cfg.ScaleDownCooldownSeconds)*time.Second {
			d.Reasons = append(d.Reasons, fmt.Sprintf(
				"scale-down cooldown: %s since last scale-down < %ds, staying at %d",
				now.Sub(s.lastDown).Round(time.Second), s.cfg.ScaleDownCooldownSeconds, s.replicas))
			desired = s.replicas
		} else if s.replicas-desired > s.cfg.ScaleDownMaxStep {
			d.Reasons = append(d.Reasons, fmt.Sprintf(
				"scale-down rate limit: wanted -%d, capped to -%d per action",
				s.replicas-desired, s.cfg.ScaleDownMaxStep))
			desired = s.replicas - s.cfg.ScaleDownMaxStep
		}
	}

	// --- 5. Min/max bounds ----------------------------------------------
	if desired < s.cfg.MinReplicas {
		d.Reasons = append(d.Reasons, fmt.Sprintf("clamped to min_replicas=%d", s.cfg.MinReplicas))
		desired = s.cfg.MinReplicas
	}
	if desired > s.cfg.MaxReplicas {
		d.Reasons = append(d.Reasons, fmt.Sprintf("clamped to max_replicas=%d", s.cfg.MaxReplicas))
		desired = s.cfg.MaxReplicas
	}

	// --- 6. Apply --------------------------------------------------------
	switch {
	case desired > s.replicas:
		d.Action = ActionScaleUp
		s.lastUp, s.hasUp = now, true
		d.Reasons = append(d.Reasons, fmt.Sprintf("scaling up %d -> %d", s.replicas, desired))
	case desired < s.replicas:
		d.Action = ActionScaleDown
		s.lastDown, s.hasDown = now, true
		d.Reasons = append(d.Reasons, fmt.Sprintf("scaling down %d -> %d", s.replicas, desired))
	default:
		d.Action = ActionHold
		if len(d.Reasons) == 0 {
			d.Reasons = append(d.Reasons, "desired equals current replicas: no action")
		}
	}
	d.DesiredReplicas = desired
	s.replicas = desired
	return d
}

// Decisions returns a copy of the decision history.
func (s *Scaler) Decisions() []Decision {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]Decision, len(s.decisions))
	copy(out, s.decisions)
	return out
}

// State returns a snapshot of the controller.
func (s *Scaler) State() State {
	s.mu.Lock()
	defer s.mu.Unlock()
	st := State{
		Replicas:      s.replicas,
		Config:        s.cfg,
		DecisionCount: len(s.decisions),
	}
	if s.hasUp {
		t := s.lastUp
		st.LastScaleUp = &t
	}
	if s.hasDown {
		t := s.lastDown
		st.LastScaleDown = &t
	}
	return st
}

// Reset clears all metrics, recommendations and decision history, and sets the
// replica count (clamped into bounds). Useful to run independent scenarios.
func (s *Scaler) Reset(replicas int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if replicas < s.cfg.MinReplicas {
		replicas = s.cfg.MinReplicas
	}
	if replicas > s.cfg.MaxReplicas {
		replicas = s.cfg.MaxReplicas
	}
	s.replicas = replicas
	s.latest = make(map[string]Sample)
	s.podStart = make(map[string]time.Time)
	s.recs = nil
	s.hasUp, s.hasDown = false, false
	s.decisions = nil
	s.seq = 0
}
