package sampler

import (
	"fmt"
	"time"
)

// Config holds all tail-sampling knobs. Durations are in wall-clock time
// as observed by the sampler's clock.
type Config struct {
	// DecisionWait is how long spans are buffered for a trace before the
	// final decision is made (the "decision wait window").
	DecisionWait time.Duration
	// LatencyThresholdMs: traces whose end-to-end duration reaches this
	// threshold are kept.
	LatencyThresholdMs int64
	// BudgetKeepsPerMin caps how many traces may be kept per rolling
	// one-minute window. 0 means unlimited.
	BudgetKeepsPerMin int
	// MaxInflightTraces caps undecided traces. When exceeded, the oldest
	// traces are force-decided immediately and marked degraded.
	MaxInflightTraces int
	// DecisionTTL is how long a final decision is remembered so that late
	// spans of the same trace get the identical verdict.
	DecisionTTL time.Duration
}

func (c Config) withDefaults() Config {
	if c.DecisionWait <= 0 {
		c.DecisionWait = 10 * time.Second
	}
	if c.LatencyThresholdMs <= 0 {
		c.LatencyThresholdMs = 500
	}
	if c.MaxInflightTraces <= 0 {
		c.MaxInflightTraces = 10000
	}
	if c.DecisionTTL <= 0 {
		c.DecisionTTL = 10 * c.DecisionWait
	}
	return c
}

// evaluate applies the keep policies to a buffered trace and returns the
// keep verdict plus the matched policy reasons (before budget checks).
func evaluatePolicies(spans []Span, cfg Config) (keep bool, reasons []string) {
	var minStart, maxEnd int64
	hasError := false
	for i, s := range spans {
		if s.Status == "error" {
			hasError = true
		}
		end := s.EndUnixMs()
		if i == 0 || s.StartUnixMs < minStart {
			minStart = s.StartUnixMs
		}
		if i == 0 || end > maxEnd {
			maxEnd = end
		}
	}
	if hasError {
		keep = true
		reasons = append(reasons, "error_span")
	}
	if d := maxEnd - minStart; len(spans) > 0 && d >= cfg.LatencyThresholdMs {
		keep = true
		reasons = append(reasons, fmt.Sprintf("latency_exceeded(%dms>=%dms)", d, cfg.LatencyThresholdMs))
	}
	return keep, reasons
}

// completeness inspects span topology and returns reasons the trace is
// considered incomplete at decision time. Empty result means complete.
func completeness(spans []Span) []string {
	ids := make(map[string]bool, len(spans))
	hasRoot := false
	for _, s := range spans {
		ids[s.SpanID] = true
		if s.ParentID == "" {
			hasRoot = true
		}
	}
	var out []string
	if !hasRoot {
		out = append(out, "root_span_missing")
	}
	missing := map[string]bool{}
	for _, s := range spans {
		if s.ParentID != "" && !ids[s.ParentID] {
			missing[s.ParentID] = true
		}
	}
	if len(missing) > 0 {
		out = append(out, fmt.Sprintf("parent_spans_missing(%d)", len(missing)))
	}
	return out
}
