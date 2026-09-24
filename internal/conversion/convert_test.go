package conversion_test

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"

	"github.com/example/crd-migration-compat/internal/conversion"
)

// loadU parses a JSON object into an unstructured. Failures abort the test
// immediately because every fixture here is static.
func loadU(t *testing.T, raw string) *unstructured.Unstructured {
	t.Helper()
	u := &unstructured.Unstructured{}
	require.NoError(t, json.Unmarshal([]byte(raw), &u.Object), "fixture must be valid JSON")
	return u
}

func mustMarshal(t *testing.T, u *unstructured.Unstructured) string {
	t.Helper()
	b, err := json.Marshal(u.Object)
	require.NoError(t, err)
	return string(b)
}

// TestV1Alpha1ToV1Basic checks the headline mapping: integer seconds become
// the structured duration, payload carries across, apiVersion flips.
func TestV1Alpha1ToV1Basic(t *testing.T) {
	in := loadU(t, `{
		"apiVersion": "migration.example.io/v1alpha1",
		"kind": "Task",
		"metadata": {"name": "demo", "namespace": "default"},
		"spec": {"timeoutSeconds": 45, "payload": "echo hi"}
	}`)

	out, err := conversion.NewConverter(conversion.Hooks{}).
		ConvertObject(context.Background(), in, conversion.V1Alpha1, conversion.V1)
	require.NoError(t, err)

	assert.Equal(t, "migration.example.io/v1", out.GetAPIVersion())
	assert.Equal(t, "Task", out.GetKind())
	spec, ok, err := unstructured.NestedMap(out.Object, "spec")
	require.NoError(t, err)
	require.True(t, ok)
	require.Contains(t, spec, "timeout")
	require.NotContains(t, spec, "timeoutSeconds")
	timeout := spec["timeout"].(map[string]any)
	assert.EqualValues(t, 45, timeout["seconds"])
	assert.Equal(t, "echo hi", spec["payload"])
}

// TestRoundTripWholeSeconds verifies a v1alpha1 object converts up to v1 and
// back without any field loss (the basic read/write round-trip).
func TestRoundTripWholeSeconds(t *testing.T) {
	in := loadU(t, `{
		"apiVersion": "migration.example.io/v1alpha1",
		"kind": "Task",
		"metadata": {"name": "rt", "namespace": "ns1", "labels": {"a": "b"}},
		"spec": {"timeoutSeconds": 90, "payload": "work"},
		"status": {"phase": "Running", "retries": 2, "message": "going"}
	}`)
	c := conversion.NewConverter(conversion.Hooks{})

	v1Obj, err := c.ConvertObject(context.Background(), in, conversion.V1Alpha1, conversion.V1)
	require.NoError(t, err)
	back, err := c.ConvertObject(context.Background(), v1Obj, conversion.V1, conversion.V1Alpha1)
	require.NoError(t, err)

	assert.JSONEq(t, mustMarshal(t, in), mustMarshal(t, back))
}

// TestNewFieldsSurviveOldClient is the central compatibility assertion:
// v1-only fields (priority, tags) move into the preserve annotation on the way
// down and reappear on the way up, so an old client that GETs as v1alpha1,
// mutates one field and writes back does not erase the new metadata.
func TestNewFieldsSurviveOldClient(t *testing.T) {
	v1Obj := loadU(t, `{
		"apiVersion": "migration.example.io/v1",
		"kind": "Task",
		"metadata": {"name": "newfields", "namespace": "default"},
		"spec": {
			"timeout": {"seconds": 60},
			"payload": "p",
			"priority": "High",
			"tags": ["batch", "gpu"]
		}
	}`)
	c := conversion.NewConverter(conversion.Hooks{})

	old, err := c.ConvertObject(context.Background(), v1Obj, conversion.V1, conversion.V1Alpha1)
	require.NoError(t, err)

	// The legacy shape has no priority/tags and carries seconds as int.
	spec, _, _ := unstructured.NestedMap(old.Object, "spec")
	assert.NotContains(t, spec, "priority")
	assert.NotContains(t, spec, "tags")
	assert.EqualValues(t, 60, spec["timeoutSeconds"])

	annos := old.GetAnnotations()
	encoded, ok := annos[conversion.AnnotationV1SpecPreserve]
	require.True(t, ok, "v1-only fields must be parked in the preserve annotation")
	assert.Contains(t, encoded, `"priority":"High"`)
	assert.Contains(t, encoded, `"gpu"`)

	// Simulate an old client changing only the payload, leaving metadata
	// (including the annotation) untouched, then converting back for storage.
	require.NoError(t, unstructured.SetNestedField(old.Object, "changed", "spec", "payload"))
	back, err := c.ConvertObject(context.Background(), old, conversion.V1Alpha1, conversion.V1)
	require.NoError(t, err)

	backSpec, _, err := unstructured.NestedMap(back.Object, "spec")
	require.NoError(t, err)
	assert.Equal(t, "High", backSpec["priority"])
	assert.Equal(t, []any{"batch", "gpu"}, backSpec["tags"])
	assert.Equal(t, "changed", backSpec["payload"])
	assert.Equal(t, map[string]any{"seconds": int64(60)}, backSpec["timeout"])
	// Annotation must not leak into the v1 representation.
	_, present := back.GetAnnotations()[conversion.AnnotationV1SpecPreserve]
	assert.False(t, present)
}

// TestSubSecondNanosRejected proves inexpressible values are errors rather
// than silent truncation: 90s + 500ms must never become 90s on the old API.
func TestSubSecondNanosRejected(t *testing.T) {
	v1Obj := loadU(t, `{
		"apiVersion": "migration.example.io/v1",
		"kind": "Task",
		"metadata": {"name": "subsecond", "namespace": "default"},
		"spec": {"timeout": {"seconds": 90, "nanos": 500000000}}
	}`)
	_, err := conversion.NewConverter(conversion.Hooks{}).
		ConvertObject(context.Background(), v1Obj, conversion.V1, conversion.V1Alpha1)
	require.Error(t, err)
	assert.True(t, conversion.IsInexpressible(err), "expected InexpressibleValue, got %v", err)
	assert.Contains(t, err.Error(), "500000000")

	// Converting the same object to its own version still works — the
	// restriction is specific to the lossy down-conversion.
	same, errSame := conversion.NewConverter(conversion.Hooks{}).
		ConvertObject(context.Background(), v1Obj, conversion.V1, conversion.V1)
	require.NoError(t, errSame)
	assert.Equal(t, "migration.example.io/v1", same.GetAPIVersion())
}

// TestInvalidDurationsRejected covers malformed/out-of-range durations in
// either direction.
func TestInvalidDurationsRejected(t *testing.T) {
	cases := []struct {
		name  string
		dir   string
		input string
		want  string
	}{
		{
			name: "negative seconds v1",
			dir:  "down",
			input: `{"apiVersion":"migration.example.io/v1","kind":"Task","metadata":{"name":"x"},
				"spec":{"timeout":{"seconds":-1}}}`,
			want: "must be >= 0",
		},
		{
			name: "nanos too big",
			dir:  "down",
			input: `{"apiVersion":"migration.example.io/v1","kind":"Task","metadata":{"name":"x"},
				"spec":{"timeout":{"seconds":1,"nanos":1000000000}}}`,
			want: "[0,999999999]",
		},
		{
			name: "seconds missing",
			dir:  "down",
			input: `{"apiVersion":"migration.example.io/v1","kind":"Task","metadata":{"name":"x"},
				"spec":{"timeout":{"nanos":1}}}`,
			want: "seconds is required",
		},
		{
			name: "timeout not an object",
			dir:  "down",
			input: `{"apiVersion":"migration.example.io/v1","kind":"Task","metadata":{"name":"x"},
				"spec":{"timeout":"30s"}}`,
			want: "must be an object",
		},
		{
			name: "negative legacy seconds",
			dir:  "up",
			input: `{"apiVersion":"migration.example.io/v1alpha1","kind":"Task","metadata":{"name":"x"},
				"spec":{"timeoutSeconds":-5}}`,
			want: "must be >= 0",
		},
		{
			name: "huge legacy seconds",
			dir:  "up",
			input: `{"apiVersion":"migration.example.io/v1alpha1","kind":"Task","metadata":{"name":"x"},
				"spec":{"timeoutSeconds":400000000000}}`,
			want: "exceeds maximum",
		},
	}
	c := conversion.NewConverter(conversion.Hooks{})
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			u := loadU(t, tc.input)
			var err error
			if tc.dir == "down" {
				_, err = c.ConvertObject(context.Background(), u, conversion.V1, conversion.V1Alpha1)
			} else {
				_, err = c.ConvertObject(context.Background(), u, conversion.V1Alpha1, conversion.V1)
			}
			require.Error(t, err)
			assert.Contains(t, err.Error(), tc.want)
			assert.False(t, conversion.IsInexpressible(err) && !strings.Contains(err.Error(), "sub-second"),
				"validation failures must not be mislabelled as InexpressibleValue: %v", err)
		})
	}
}

// TestTimeoutHonoursContext exercises the context-deadline path directly via
// the PreConvert hook: once the context has expired, conversion fails with a
// ConversionCancelled error and never returns a partial object.
func TestTimeoutHonoursContext(t *testing.T) {
	slow := conversion.NewConverter(conversion.Hooks{
		PreConvert: func(ctx context.Context, dir conversion.Direction, name string) error {
			select {
			case <-time.After(200 * time.Millisecond):
			case <-ctx.Done():
				return ctx.Err()
			}
			return nil
		},
	})
	in := loadU(t, `{"apiVersion":"migration.example.io/v1alpha1","kind":"Task",
		"metadata":{"name":"slow"},"spec":{"timeoutSeconds":1}}`)

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	start := time.Now()
	_, err := slow.ConvertObject(ctx, in, conversion.V1Alpha1, conversion.V1)
	elapsed := time.Since(start)

	require.Error(t, err)
	assert.Contains(t, err.Error(), "ConversionCancelled")
	assert.Less(t, elapsed, 180*time.Millisecond, "must return promptly when cancelled")
}

// TestAlreadyExpiredContext proves even an immediately expired context is
// honoured (cheap deterministic test, no sleeps).
func TestAlreadyExpiredContext(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	in := loadU(t, `{"apiVersion":"migration.example.io/v1alpha1","kind":"Task","metadata":{"name":"x"}}`)
	_, err := conversion.NewConverter(conversion.Hooks{}).
		ConvertObject(ctx, in, conversion.V1Alpha1, conversion.V1)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "ConversionCancelled")
}

// TestUnknownFieldsPassThrough verifies fields that neither version models
// survive conversion on both directions. The CRD schema explicitly opts into
// x-kubernetes-preserve-unknown-fields (see config/crd/*.yaml) and this is
// the converter-side half of that guarantee.
func TestUnknownFieldsPassThrough(t *testing.T) {
	in := loadU(t, `{
		"apiVersion": "migration.example.io/v1alpha1",
		"kind": "Task",
		"metadata": {"name": "unknown"},
		"spec": {"timeoutSeconds": 1, "experimentalFlux": {"mode": "fast", "ratio": 0.25}},
		"status": {"phase": "Pending", "customStatusField": [1, 2, 3]}
	}`)
	c := conversion.NewConverter(conversion.Hooks{})

	v1Obj, err := c.ConvertObject(context.Background(), in, conversion.V1Alpha1, conversion.V1)
	require.NoError(t, err)
	flux, ok, err := unstructured.NestedMap(v1Obj.Object, "spec", "experimentalFlux")
	require.NoError(t, err)
	require.True(t, ok, "unknown spec field must be carried to v1")
	assert.Equal(t, "fast", flux["mode"])

	back, err := c.ConvertObject(context.Background(), v1Obj, conversion.V1, conversion.V1Alpha1)
	require.NoError(t, err)
	flux2, ok, err := unstructured.NestedMap(back.Object, "spec", "experimentalFlux")
	require.NoError(t, err)
	require.True(t, ok)
	assert.Equal(t, 0.25, flux2["ratio"])

	custom, ok, _ := unstructured.NestedSlice(back.Object, "status", "customStatusField")
	require.True(t, ok)
	assert.Len(t, custom, 3)
}

// TestInputNotMutated ensures conversion leaves its argument intact (it must
// be safe to reuse the input afterwards, as the webhook does).
func TestInputNotMutated(t *testing.T) {
	in := loadU(t, `{
		"apiVersion":"migration.example.io/v1","kind":"Task","metadata":{"name":"n"},
		"spec":{"timeout":{"seconds":5},"priority":"Low","tags":["a"]}}`)
	before := mustMarshal(t, in)
	_, err := conversion.NewConverter(conversion.Hooks{}).
		ConvertObject(context.Background(), in, conversion.V1, conversion.V1Alpha1)
	require.NoError(t, err)
	assert.JSONEq(t, before, mustMarshal(t, in))
}

// TestMalformedPreserveAnnotation ensures a corrupted preserve annotation is
// rejected loudly instead of silently dropping the parked v1 fields.
func TestMalformedPreserveAnnotation(t *testing.T) {
	old := loadU(t, `{
		"apiVersion":"migration.example.io/v1alpha1","kind":"Task","metadata":{"name":"n",
		"annotations":{"migration.example.io/v1-spec-preserve":"{not-json"}},
		"spec":{"timeoutSeconds":5}}`)
	_, err := conversion.NewConverter(conversion.Hooks{}).
		ConvertObject(context.Background(), old, conversion.V1Alpha1, conversion.V1)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "MalformedPreserveAnnotation")
}

// TestDefaultsRepresentable confirms zero/omitted timeout behaviour: an
// absent timeoutSeconds maps to no timeout key (defaulting is the admission
// webhook's job, not conversion's), and the same holds on the way down.
func TestAbsentTimeout(t *testing.T) {
	in := loadU(t, `{"apiVersion":"migration.example.io/v1alpha1","kind":"Task",
		"metadata":{"name":"n"},"spec":{"payload":"x"}}`)
	c := conversion.NewConverter(conversion.Hooks{})
	v1Obj, err := c.ConvertObject(context.Background(), in, conversion.V1Alpha1, conversion.V1)
	require.NoError(t, err)
	spec, _, _ := unstructured.NestedMap(v1Obj.Object, "spec")
	assert.NotContains(t, spec, "timeout")

	back, err := c.ConvertObject(context.Background(), v1Obj, conversion.V1, conversion.V1Alpha1)
	require.NoError(t, err)
	backSpec, _, _ := unstructured.NestedMap(back.Object, "spec")
	assert.NotContains(t, backSpec, "timeoutSeconds")
}
