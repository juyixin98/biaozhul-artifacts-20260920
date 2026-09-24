// Package conversion implements the v1alpha1 <-> v1 conversion semantics for
// the Task custom resource.
//
// It deliberately works on unstructured objects (decoded JSON maps) instead
// of on the typed Go structs: typed conversion re-marshals the object through
// the destination struct and drops keys that are not part of it, which is
// exactly how an old client silently wipes new fields. Working on the raw maps
// lets us:
//
//   - map the fields that exist in both versions,
//   - move fields that only exist in v1 into a reserved annotation so they
//     survive a write by a v1alpha1 client,
//   - and leave every truly unknown key untouched (the CRD schema explicitly
//     opts into preserving unknown fields, see
//     config/crd/tasks.migration.example.io.yaml).
package conversion

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
)

// AnnotationV1SpecPreserve is the reserved annotation that carries v1-only
// spec fields while an object is represented as v1alpha1. Its value is a JSON
// object containing a "spec" map with exactly the v1 spec keys that have no
// v1alpha1 counterpart (today: priority and tags).
//
// The annotation is owned by the conversion webhook. It is always rewritten
// on v1 -> v1alpha1 conversion and consumed (and removed) on the way back, so
// it never leaks into v1.
const AnnotationV1SpecPreserve = "migration.example.io/v1-spec-preserve"

// Versions supported by this package.
const (
	V1Alpha1 = "v1alpha1"
	V1       = "v1"
)

// Direction identifies a requested conversion, used in error messages.
type Direction string

const (
	// DirUp is v1alpha1 -> v1.
	DirUp Direction = "v1alpha1->v1"
	// DirDown is v1 -> v1alpha1.
	DirDown Direction = "v1->v1alpha1"
)

// ConversionError marks a conversion failure with a stable reason. The
// webhook maps reasons to HTTP status codes; clients should inspect
// errors.Is rather than string-match.
type ConversionError struct {
	// Reason classifies the failure, e.g. "InexpressibleValue".
	Reason string
	// Message is the human-readable detail.
	Message string
}

func (e *ConversionError) Error() string {
	return fmt.Sprintf("%s: %s", e.Reason, e.Message)
}

// Stable reason strings, also surfaced in ConversionReview responses.
const (
	ReasonInexpressibleValue = "InexpressibleValue"
	ReasonInvalidField       = "InvalidField"
	ReasonMalformedPreserve  = "MalformedPreserveAnnotation"
	ReasonContextDone        = "ConversionCancelled"
)

func newError(reason, format string, args ...any) error {
	return &ConversionError{Reason: reason, Message: fmt.Sprintf(format, args...)}
}

// IsInexpressible reports whether err is an inexpressible-value conversion
// error.
func IsInexpressible(err error) bool {
	var ce *ConversionError
	return errors.As(err, &ce) && ce.Reason == ReasonInexpressibleValue
}

// Hooks contains optional instrumentation for the converter. Production code
// leaves it zero-valued; tests use PreConvert to inject latency and exercise
// the context-deadline path.
type Hooks struct {
	// PreConvert runs before every object is converted. If it returns an
	// error that error is reported verbatim; it may also block until ctx is
	// done to simulate a slow downstream.
	PreConvert func(ctx context.Context, dir Direction, name string) error
}

// Converter converts Task objects between the two served versions.
type Converter struct {
	hooks Hooks
}

// NewConverter builds a Converter.
func NewConverter(hooks Hooks) *Converter {
	return &Converter{hooks: hooks}
}

// ConvertObject converts a single unstructured Task between fromVersion and
// toVersion. The returned object is a fresh value; the input is never
// mutated. The context is honoured: an expired/cancelled context produces a
// ReasonContextDone error instead of a partial result.
func (c *Converter) ConvertObject(ctx context.Context, in *unstructured.Unstructured, fromVersion, toVersion string) (*unstructured.Unstructured, error) {
	if err := ctx.Err(); err != nil {
		return nil, contextError(err)
	}
	if fromVersion == toVersion {
		return in.DeepCopy(), nil
	}

	name := in.GetName()
	if c.hooks.PreConvert != nil {
		if err := c.hooks.PreConvert(ctx, direction(fromVersion, toVersion), name); err != nil {
			if ctx.Err() != nil {
				return nil, contextError(ctx.Err())
			}
			return nil, err
		}
		if err := ctx.Err(); err != nil {
			return nil, contextError(err)
		}
	}

	raw := in.UnstructuredContent()
	out, err := deepMap(raw)
	if err != nil {
		return nil, err
	}

	switch {
	case fromVersion == V1Alpha1 && toVersion == V1:
		err = convertUp(out)
	case fromVersion == V1 && toVersion == V1Alpha1:
		err = convertDown(ctx, out)
	default:
		err = newError(ReasonInvalidField, "unsupported conversion %s -> %s", fromVersion, toVersion)
	}
	if err != nil {
		return nil, err
	}

	setAPIVersion(out, "migration.example.io/"+toVersion)
	return &unstructured.Unstructured{Object: out}, nil
}

func direction(from, to string) Direction {
	if from == V1Alpha1 && to == V1 {
		return DirUp
	}
	return DirDown
}

func contextError(err error) error {
	return newError(ReasonContextDone, "conversion not performed: %v", err)
}

// convertUp rewrites a v1alpha1 object (already deep-copied into out) in place
// into v1 shape.
func convertUp(out map[string]any) error {
	spec, _ := out["spec"].(map[string]any)
	if spec == nil {
		spec = map[string]any{}
	}

	var preserved map[string]any
	if annos, _ := out["metadata"].(map[string]any); annos != nil {
		if raw, ok := annos["annotations"].(map[string]any); ok {
			if encoded, ok := raw[AnnotationV1SpecPreserve].(string); ok && encoded != "" {
				p, err := decodePreserve(encoded)
				if err != nil {
					return err
				}
				preserved = p
				delete(raw, AnnotationV1SpecPreserve)
				if len(raw) == 0 {
					delete(annos, "annotations")
				}
			}
		}
	}

	// Integer seconds -> structured Duration.
	var seconds int64
	hasSeconds := false
	switch v := spec["timeoutSeconds"].(type) {
	case nil:
	case int64:
		seconds = v
		hasSeconds = true
	case int:
		seconds = int64(v)
		hasSeconds = true
	case float64: // JSON unmarshalling without int64 detection uses float64
		if v != float64(int64(v)) {
			return newError(ReasonInvalidField, "spec.timeoutSeconds must be an integer, got %v", v)
		}
		seconds = int64(v)
		hasSeconds = true
	default:
		return newError(ReasonInvalidField, "spec.timeoutSeconds must be an integer, got %T", v)
	}
	delete(spec, "timeoutSeconds")

	if hasSeconds {
		if err := validateWholeSeconds(seconds); err != nil {
			return err
		}
		spec["timeout"] = map[string]any{"seconds": seconds}
	}

	// Restore v1-only keys carried in the preserve annotation. These are keys
	// that existed in v1 when the object was last served as v1alpha1.
	for k, v := range preserved {
		spec[k] = v
	}

	if len(spec) > 0 {
		out["spec"] = spec
	}
	return nil
}

// convertDown rewrites a v1 object (already deep-copied into out) in place
// into v1alpha1 shape.
func convertDown(ctx context.Context, out map[string]any) error {
	if err := ctx.Err(); err != nil {
		return contextError(err)
	}
	spec, _ := out["spec"].(map[string]any)
	if spec == nil {
		spec = map[string]any{}
	}

	// Structured Duration -> integer seconds. Sub-second precision is not
	// representable in v1alpha1: fail loudly, never truncate.
	if t, ok := spec["timeout"]; ok && t != nil {
		d, _ := t.(map[string]any)
		if err := validateDurationMap(d); err != nil {
			return err
		}
		if nanosOf(d) != 0 {
			return newError(ReasonInexpressibleValue,
				"spec.timeout has sub-second component %dns but v1alpha1 only supports whole seconds; "+
					"remove the nanoseconds before serving this object to old clients", nanosOf(d))
		}
		spec["timeoutSeconds"] = d["seconds"]
		delete(spec, "timeout")
	}

	// v1-only known fields -> preserve annotation.
	preserve := map[string]any{}
	for _, k := range []string{"priority", "tags"} {
		if v, ok := spec[k]; ok {
			preserve[k] = v
			delete(spec, k)
		}
	}
	if len(preserve) > 0 {
		if err := encodePreserveOnto(out, preserve); err != nil {
			return err
		}
	}

	if len(spec) > 0 {
		out["spec"] = spec
	}
	return nil
}

// validateWholeSeconds rejects values that could never describe a legal
// timeout; it keeps parity with v1 validation so a bad legacy value cannot be
// smuggled through the webhook.
func validateWholeSeconds(seconds int64) error {
	if seconds < 0 {
		return newError(ReasonInvalidField, "timeoutSeconds must be >= 0, got %d", seconds)
	}
	if seconds > 315_576_000_000 {
		return newError(ReasonInvalidField, "timeoutSeconds %d exceeds maximum 315576000000", seconds)
	}
	return nil
}

func nanosOf(d map[string]any) int64 {
	switch n := d["nanos"].(type) {
	case int64:
		return n
	case int:
		return int64(n)
	case float64:
		return int64(n)
	default:
		return 0
	}
}

// validateDurationMap validates a decoded structured duration against the
// v1 rules (non-negative, finite seconds, nanos in [0, 1e9)).
func validateDurationMap(d map[string]any) error {
	if d == nil {
		return newError(ReasonInvalidField, "spec.timeout must be an object")
	}
	var seconds int64
	switch s := d["seconds"].(type) {
	case nil:
		return newError(ReasonInvalidField, "spec.timeout.seconds is required")
	case int64:
		seconds = s
	case int:
		seconds = int64(s)
	case float64:
		if s != float64(int64(s)) {
			return newError(ReasonInvalidField, "spec.timeout.seconds must be an integer, got %v", s)
		}
		seconds = int64(s)
	default:
		return newError(ReasonInvalidField, "spec.timeout.seconds must be an integer")
	}
	if seconds < 0 {
		return newError(ReasonInvalidField, "spec.timeout.seconds must be >= 0, got %d", seconds)
	}
	if seconds > 315_576_000_000 {
		return newError(ReasonInvalidField, "spec.timeout.seconds %d exceeds maximum 315576000000", seconds)
	}
	nanos := nanosOf(d)
	if nanos < 0 || nanos >= 1_000_000_000 {
		return newError(ReasonInvalidField, "spec.timeout.nanos must be in [0,999999999], got %d", nanos)
	}
	return nil
}

// decodePreserve parses the v1-spec-preserve annotation and returns its
// "spec" object.
func decodePreserve(encoded string) (map[string]any, error) {
	var wrapper struct {
		APIVersion string         `json:"apiVersion"`
		Kind       string         `json:"kind"`
		Spec       map[string]any `json:"spec"`
	}
	dec := json.NewDecoder(strings.NewReader(encoded))
	dec.UseNumber()
	if err := dec.Decode(&wrapper); err != nil {
		return nil, newError(ReasonMalformedPreserve, "annotation %q is not valid JSON: %v", AnnotationV1SpecPreserve, err)
	}
	if wrapper.APIVersion != "migration.example.io/v1" || wrapper.Kind != "Task" {
		return nil, newError(ReasonMalformedPreserve,
			"annotation %q must wrap migration.example.io/v1 Task, got %s/%s",
			AnnotationV1SpecPreserve, wrapper.APIVersion, wrapper.Kind)
	}
	return wrapper.Spec, nil
}

// encodePreserveOnto writes specFields into the preserve annotation of out.
func encodePreserveOnto(out map[string]any, specFields map[string]any) error {
	meta, _ := out["metadata"].(map[string]any)
	if meta == nil {
		meta = map[string]any{}
		out["metadata"] = meta
	}
	annos, _ := meta["annotations"].(map[string]any)
	if annos == nil {
		annos = map[string]any{}
		meta["annotations"] = annos
	}

	// Merge with an existing annotation rather than clobbering it; in practice
	// the webhook always owns this key, but merging keeps the converter total.
	if existing, ok := annos[AnnotationV1SpecPreserve].(string); ok && existing != "" {
		prev, err := decodePreserve(existing)
		if err != nil {
			return err
		}
		for k, v := range prev {
			if _, conflict := specFields[k]; !conflict {
				specFields[k] = v
			}
		}
	}

	wrapper := map[string]any{
		"apiVersion": "migration.example.io/v1",
		"kind":       "Task",
		"spec":       specFields,
	}
	encoded, err := json.Marshal(wrapper)
	if err != nil {
		return newError(ReasonMalformedPreserve, "cannot encode preserve annotation: %v", err)
	}
	annos[AnnotationV1SpecPreserve] = string(encoded)
	return nil
}

// setAPIVersion replaces the apiVersion key while leaving kind untouched.
func setAPIVersion(out map[string]any, apiVersion string) {
	out["apiVersion"] = apiVersion
}

// deepMap returns a value-deep copy of a decoded JSON tree so the converter
// never mutates the input.
func deepMap(in map[string]any) (map[string]any, error) {
	b, err := json.Marshal(&unstructured.Unstructured{Object: in})
	if err != nil {
		return nil, fmt.Errorf("conversion: cannot snapshot input: %w", err)
	}
	var out map[string]any
	dec := json.NewDecoder(strings.NewReader(string(b)))
	dec.UseNumber()
	if err := dec.Decode(&out); err != nil {
		return nil, fmt.Errorf("conversion: cannot decode snapshot: %w", err)
	}
	normalizeNumbers(out)
	return out, nil
}

// normalizeNumbers turns json.Number integral values back into int64 to match
// what unstructured helpers expect; non-integral numbers stay float64.
func normalizeNumbers(v any) {
	switch t := v.(type) {
	case map[string]any:
		for k, val := range t {
			switch n := val.(type) {
			case json.Number:
				t[k] = decodeJSONNumber(n)
			default:
				normalizeNumbers(val)
			}
		}
	case []any:
		for i, val := range t {
			switch n := val.(type) {
			case json.Number:
				t[i] = decodeJSONNumber(n)
			default:
				normalizeNumbers(val)
			}
		}
	}
}

func decodeJSONNumber(n json.Number) any {
	if i, err := n.Int64(); err == nil {
		return i
	}
	if f, err := n.Float64(); err == nil {
		return f
	}
	return n.String()
}
