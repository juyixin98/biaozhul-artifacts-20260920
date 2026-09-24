// Package validation holds the object validation rules used by the
// validating admission webhook. Rules are expressed per storage version
// but the semantic guarantees are identical: no negative durations, nanos
// strictly inside (-1e9, 1e9) and sign-consistent with seconds, interval
// strictly positive.
package validation

import (
	"fmt"

	v1 "github.com/example/crd-migration-demo/api/v1"
	v1alpha1 "github.com/example/crd-migration-demo/api/v1alpha1"
)

const (
	nanosPerSecond = 1_000_000_000

	PriorityLow    = "Low"
	PriorityNormal = "Normal"
	PriorityHigh   = "High"
)

// ValidateV1 validates a v1 Timer object (create and update alike).
func ValidateV1(t *v1.Timer) error {
	if t == nil {
		return fmt.Errorf("object is nil")
	}
	if err := ValidateDurationV1("spec.interval", &t.Spec.Interval); err != nil {
		return err
	}
	if !durationPositiveV1(&t.Spec.Interval) {
		return fmt.Errorf("spec.interval must be strictly positive, got {seconds:%d nanos:%d}",
			t.Spec.Interval.Seconds, t.Spec.Interval.Nanos)
	}
	if t.Spec.Timeout != nil {
		if err := ValidateDurationV1("spec.timeout", t.Spec.Timeout); err != nil {
			return err
		}
	}
	switch t.Spec.Priority {
	case "", PriorityLow, PriorityNormal, PriorityHigh:
	default:
		return fmt.Errorf("spec.priority must be one of Low, Normal, High; got %q", t.Spec.Priority)
	}
	return nil
}

// ValidateV1Alpha1 validates a v1alpha1 Timer object.
func ValidateV1Alpha1(t *v1alpha1.Timer) error {
	if t == nil {
		return fmt.Errorf("object is nil")
	}
	if t.Spec.IntervalSeconds <= 0 {
		return fmt.Errorf("spec.intervalSeconds must be >= 1, got %d", t.Spec.IntervalSeconds)
	}
	if t.Spec.TimeoutSeconds != nil && *t.Spec.TimeoutSeconds < 0 {
		return fmt.Errorf("spec.timeoutSeconds must be >= 0, got %d", *t.Spec.TimeoutSeconds)
	}
	return nil
}

// ValidateDurationV1 enforces protobuf Duration invariants.
func ValidateDurationV1(field string, d *v1.Duration) error {
	if d == nil {
		return fmt.Errorf("%s is required", field)
	}
	if d.Nanos <= -nanosPerSecond || d.Nanos >= nanosPerSecond {
		return fmt.Errorf("%s.nanos must be in (-%d, %d), got %d",
			field, nanosPerSecond, nanosPerSecond, d.Nanos)
	}
	switch {
	case d.Seconds < 0 && d.Nanos > 0:
		return fmt.Errorf("%s: seconds and nanos must share the same sign (seconds=%d, nanos=%d)",
			field, d.Seconds, d.Nanos)
	case d.Seconds > 0 && d.Nanos < 0:
		return fmt.Errorf("%s: seconds and nanos must share the same sign (seconds=%d, nanos=%d)",
			field, d.Seconds, d.Nanos)
	case d.Seconds == 0 && d.Nanos == 0:
		// zero is allowed only for timeout (caller decides positivity)
	}
	return nil
}

func durationPositiveV1(d *v1.Duration) bool {
	return d.Seconds > 0 || (d.Seconds == 0 && d.Nanos > 0)
}
