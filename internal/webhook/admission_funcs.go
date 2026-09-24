package webhook

import (
	"fmt"

	admissionv1 "k8s.io/api/admission/v1"
)

// defaultTimeoutSeconds is applied when a Task is created without any
// timeout value (spec.timeoutSeconds in v1alpha1, spec.timeout in v1).
const defaultTimeoutSeconds int64 = 30

// mutateV1 defaults v1 Task objects on create (and, defensively, on update if
// a client removes the fields). It never rewrites fields that are present.
func mutateV1(op admissionv1.Operation, raw map[string]any) (map[string]any, []string) {
	patch := map[string]any{}
	spec, _ := raw["spec"].(map[string]any)
	if spec == nil {
		spec = map[string]any{}
	}

	if _, ok := spec["timeout"]; !ok {
		patch["spec"] = mergePatchSpec(patch, map[string]any{
			"timeout": map[string]any{"seconds": defaultTimeoutSeconds},
		})
	}
	if _, ok := spec["priority"]; !ok {
		patch["spec"] = mergePatchSpec(patch, map[string]any{
			"priority": "Normal",
		})
	}
	return patch, nil
}

// mutateV1Alpha1 defaults legacy Task objects: missing timeoutSeconds becomes
// 30. v1alpha1 has no priority/tags to default.
func mutateV1Alpha1(op admissionv1.Operation, raw map[string]any) (map[string]any, []string) {
	patch := map[string]any{}
	spec, _ := raw["spec"].(map[string]any)
	if spec == nil {
		spec = map[string]any{}
	}
	if _, ok := spec["timeoutSeconds"]; !ok {
		patch["spec"] = map[string]any{"timeoutSeconds": defaultTimeoutSeconds}
	}
	return patch, nil
}

// mergePatchSpec merges new keys into a possibly already-present sparse
// "spec" patch object so two defaults end up in one merge patch.
func mergePatchSpec(patch map[string]any, add map[string]any) map[string]any {
	specPatch, _ := patch["spec"].(map[string]any)
	if specPatch == nil {
		specPatch = map[string]any{}
	}
	for k, v := range add {
		specPatch[k] = v
	}
	return specPatch
}

// validateV1 enforces the v1 schema rules that matter for conversion safety:
// structured duration shape and priority enum. The structural CRD schema is
// the first line of defence; this webhook gives explicit, version-aware
// errors (and covers requests that bypass schema enforcement).
func validateV1(op admissionv1.Operation, raw map[string]any) (map[string]any, []string) {
	var failures []string
	spec, _ := raw["spec"].(map[string]any)
	if spec == nil {
		return nil, nil
	}
	if t, ok := spec["timeout"]; ok && t != nil {
		d, ok := t.(map[string]any)
		if !ok {
			failures = append(failures, "spec.timeout must be an object with seconds and optional nanos")
		} else {
			failures = append(failures, validateDurationJSON(d)...)
		}
	}
	if p, ok := spec["priority"]; ok {
		s, _ := p.(string)
		if s != "" && s != "Normal" && s != "High" && s != "Low" {
			failures = append(failures, fmt.Sprintf("spec.priority must be one of Normal|High|Low, got %q", s))
		}
	}
	if tags, ok := spec["tags"]; ok && tags != nil {
		if _, isArray := tags.([]any); !isArray {
			failures = append(failures, "spec.tags must be an array of strings")
		}
	}
	return nil, failures
}

// validateV1Alpha1 validates the legacy integer-seconds field.
func validateV1Alpha1(op admissionv1.Operation, raw map[string]any) (map[string]any, []string) {
	var failures []string
	spec, _ := raw["spec"].(map[string]any)
	if spec == nil {
		return nil, nil
	}
	if v, ok := spec["timeoutSeconds"]; ok && v != nil {
		s, ok := numToInt64(v)
		if !ok {
			failures = append(failures, "spec.timeoutSeconds must be an integer")
		} else if err := checkWholeSeconds(s); err != nil {
			failures = append(failures, err.Error())
		}
	}
	return nil, failures
}

func validateDurationJSON(d map[string]any) []string {
	var failures []string
	secondsRaw, present := d["seconds"]
	if !present {
		failures = append(failures, "spec.timeout.seconds is required")
		return failures
	}
	seconds, ok := numToInt64(secondsRaw)
	if !ok {
		failures = append(failures, "spec.timeout.seconds must be an integer")
	} else if err := checkWholeSeconds(seconds); err != nil {
		failures = append(failures, err.Error())
	}
	if n, ok := d["nanos"]; ok && n != nil {
		nanos, ok := numToInt64(n)
		if !ok {
			failures = append(failures, "spec.timeout.nanos must be an integer")
		} else if nanos < 0 || nanos >= 1_000_000_000 {
			failures = append(failures, fmt.Sprintf("spec.timeout.nanos must be within [0,999999999], got %d", nanos))
		}
	}
	return failures
}

func checkWholeSeconds(s int64) error {
	if s < 0 {
		return fmt.Errorf("timeout seconds must be >= 0, got %d", s)
	}
	if s > 315_576_000_000 {
		return fmt.Errorf("timeout seconds %d exceed maximum 315576000000", s)
	}
	return nil
}

// numToInt64 accepts the numeric shapes JSON decoding can produce for an
// integral number and rejects non-integral floats.
func numToInt64(v any) (int64, bool) {
	switch n := v.(type) {
	case int64:
		return n, true
	case int:
		return int64(n), true
	case int32:
		return int64(n), true
	case float64:
		if n == float64(int64(n)) {
			return int64(n), true
		}
	}
	return 0, false
}
