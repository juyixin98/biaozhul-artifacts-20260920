// Package conversion implements the Timer CRD conversion webhook logic.
//
// v1 is the hub (storage version); v1alpha1 is the spoke. The mapping:
//
//	v1alpha1 intervalSeconds (int64 whole seconds) <-> v1 interval {seconds,nanos}
//	v1alpha1 timeoutSeconds  (*int64)             <-> v1 timeout  {seconds,nanos}
//
// Round-trip / preservation strategy
//
//   - spec.priority is NEW in v1 and has no v1alpha1 representation. It is
//     stashed in the annotation "timer.example.com/v1-priority" while the
//     object is served as v1alpha1, and restored on the way back. Because
//     annotations are part of object metadata, an old v1alpha1-only client
//     performing read/modify/update preserves the value automatically.
//   - spec.extra is a free-form JSON bag with
//     x-kubernetes-preserve-unknown-fields enabled in BOTH CRD schemas, so
//     every key round-trips without loss.
//   - Top-level (root and spec) unknown fields are PRUNED: the CRDs are
//     structural with preserveUnknownFields: false. This is deliberate and
//     explicit; see config/crd/bases/timer.example.com_timers.yaml.
//
// No silent truncation: a v1 value carrying sub-second precision cannot be
// expressed in v1alpha1; ConvertV1ToV1Alpha1 returns an error instead of
// dropping the nanos. The API server surfaces this as a failed conversion
// (4xx on read; failed storage migration for that object).
package conversion

import (
	"fmt"

	v1 "github.com/example/crd-migration-demo/api/v1"
	v1alpha1 "github.com/example/crd-migration-demo/api/v1alpha1"
	"github.com/example/crd-migration-demo/internal/validation"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
)

// Annotation keys.
const (
	// V1PriorityAnnotation carries spec.priority across v1alpha1 views.
	V1PriorityAnnotation = "timer.example.com/v1-priority"

	// InjectDelayAnnotation is a test/ops hook. When present on an object
	// the conversion webhook sleeps for the parsed duration before serving
	// the conversion for THAT object, emulating a slow/hung webhook. The
	// value is a Go duration (time.ParseDuration), e.g. "35s". It is never
	// part of the CRD schema and is ignored on objects without it.
	InjectDelayAnnotation = "timer.example.com/inject-conversion-delay"
)

// apiextJSON is the JSON wrapper type used by the free-form extra bag.
type apiextJSON = apiextensionsv1.JSON

// ConvertV1Alpha1ToV1 maps the spoke object onto the hub. Whole seconds
// map 1:1; missing timeout becomes nil; priority is restored from the
// round-trip annotation when present.
func ConvertV1Alpha1ToV1(src *v1alpha1.Timer, dst *v1.Timer) error {
	if src == nil {
		return fmt.Errorf("source is nil")
	}
	if dst == nil {
		return fmt.Errorf("destination is nil")
	}

	dst.ObjectMeta = *src.ObjectMeta.DeepCopy()

	dst.Spec = v1.TimerSpec{
		Interval: v1.Duration{Seconds: src.Spec.IntervalSeconds},
		Extra:    cloneExtraV1Alpha1(src.Spec.Extra),
	}
	if src.Spec.TimeoutSeconds != nil {
		dst.Spec.Timeout = &v1.Duration{Seconds: *src.Spec.TimeoutSeconds}
	}

	switch p := dst.Annotations[V1PriorityAnnotation]; p {
	case "":
		// No annotation: nothing stored from a previous v1 view. Leave
		// priority empty; the mutating webhook defaults it on create.
	case validation.PriorityLow, validation.PriorityNormal, validation.PriorityHigh:
		dst.Spec.Priority = p
	default:
		return fmt.Errorf("corrupt %s annotation: %q is not a valid priority",
			V1PriorityAnnotation, p)
	}

	dst.Status = convertStatusUp(src.Status)
	return nil
}

// ConvertV1ToV1Alpha1 maps the hub onto the spoke. Sub-second precision is
// NOT representable in v1alpha1; rather than silently truncating, the
// function returns an error. (The caller decides how to surface it: failed
// conversion response, failed migration record entry, etc.)
func ConvertV1ToV1Alpha1(src *v1.Timer, dst *v1alpha1.Timer) error {
	if src == nil {
		return fmt.Errorf("source is nil")
	}
	if dst == nil {
		return fmt.Errorf("destination is nil")
	}

	if err := ensureWholeSeconds("spec.interval", &src.Spec.Interval); err != nil {
		return err
	}
	if src.Spec.Timeout != nil {
		if err := ensureWholeSeconds("spec.timeout", src.Spec.Timeout); err != nil {
			return err
		}
	}

	dst.ObjectMeta = *src.ObjectMeta.DeepCopy()
	if dst.Annotations == nil {
		dst.Annotations = map[string]string{}
	}

	// Round-trip strategy for the v1-only field: persist in metadata so an
	// old client's update cannot erase it. Empty/normal-default values are
	// stored too so the semantic default ("Normal") is explicit.
	if src.Spec.Priority != "" {
		dst.Annotations[V1PriorityAnnotation] = src.Spec.Priority
	}

	dst.Spec = v1alpha1.TimerSpec{
		IntervalSeconds: src.Spec.Interval.Seconds,
		Extra:           cloneExtraV1(src.Spec.Extra),
	}
	if src.Spec.Timeout != nil {
		secs := src.Spec.Timeout.Seconds
		dst.Spec.TimeoutSeconds = &secs
	}

	dst.Status = convertStatusDown(src.Status)
	return nil
}

// ensureWholeSeconds returns an error when d carries sub-second precision
// that v1alpha1 cannot represent. Negative whole seconds are rejected
// separately by validation; here we only guard the representability loss.
func ensureWholeSeconds(field string, d *v1.Duration) error {
	if d == nil {
		return nil
	}
	if d.Nanos != 0 {
		return fmt.Errorf(
			"%s={seconds:%d,nanos:%d} cannot be expressed in v1alpha1 (whole seconds only); "+
				"sub-second precision must be rounded explicitly before downgrade, it is never truncated implicitly",
			field, d.Seconds, d.Nanos)
	}
	return nil
}

func convertStatusUp(s v1alpha1.TimerStatus) v1.TimerStatus {
	out := v1.TimerStatus{}
	if s.LastFired != nil {
		out.LastFired = s.LastFired.DeepCopy()
	}
	return out
}

func convertStatusDown(s v1.TimerStatus) v1alpha1.TimerStatus {
	out := v1alpha1.TimerStatus{}
	if s.LastFired != nil {
		out.LastFired = s.LastFired.DeepCopy()
	}
	return out
}

func cloneExtraV1Alpha1(in map[string]apiextJSON) map[string]apiextJSON {
	if in == nil {
		return nil
	}
	out := make(map[string]apiextJSON, len(in))
	for k, v := range in {
		out[k] = *v.DeepCopy()
	}
	return out
}

func cloneExtraV1(in map[string]apiextJSON) map[string]apiextJSON {
	return cloneExtraV1Alpha1(in)
}
