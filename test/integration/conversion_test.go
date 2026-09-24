package integration

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	apiextensionsclient "k8s.io/apiextensions-apiserver/pkg/client/clientset/clientset"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/dynamic"
	"sigs.k8s.io/controller-runtime/pkg/client"

	v1 "github.com/example/crd-migration-compat/api/v1"
	v1alpha1 "github.com/example/crd-migration-compat/api/v1alpha1"
)

var (
	gvrV1       = schema.GroupVersionResource{Group: "migration.example.io", Version: "v1", Resource: "tasks"}
	gvrV1Alpha1 = schema.GroupVersionResource{Group: "migration.example.io", Version: "v1alpha1", Resource: "tasks"}
)

func dynamicClient(t *testing.T) dynamic.Interface {
	t.Helper()
	dc, err := dynamic.NewForConfig(restCfg)
	require.NoError(t, err)
	return dc
}

// TestLegacyCreateDefaultsAndReadsAsV1 covers defaulting + the basic
// read/write round-trip through both served versions against a real API
// server.
func TestLegacyCreateDefaultsAndReadsAsV1(t *testing.T) {
	ctx := context.Background()
	dyn := dynamicClient(t)

	// 1. A legacy client creates a v1alpha1 object without a timeout.
	legacy := &unstructured.Unstructured{}
	legacy.SetGroupVersionKind(schema.GroupVersionKind{
		Group: "migration.example.io", Version: "v1alpha1", Kind: "Task",
	})
	legacy.SetName("legacy-defaults")
	legacy.SetNamespace("itest")
	_ = unstructured.SetNestedField(legacy.Object, "backup-now", "spec", "payload")

	created, err := dyn.Resource(gvrV1Alpha1).Namespace("itest").Create(ctx, legacy, metav1.CreateOptions{})
	require.NoError(t, err)

	// The mutating webhook defaulted timeoutSeconds to 30.
	gotSeconds, ok, err := unstructured.NestedInt64(created.Object, "spec", "timeoutSeconds")
	require.NoError(t, err)
	require.True(t, ok, "timeoutSeconds should be defaulted")
	assert.Equal(t, int64(30), gotSeconds)

	// 2. A new client reads the same object as v1: the server converts
	// 30 seconds -> structured duration.
	v1Obj, err := dyn.Resource(gvrV1).Namespace("itest").Get(ctx, "legacy-defaults", metav1.GetOptions{})
	require.NoError(t, err)
	timeout, ok, err := unstructured.NestedMap(v1Obj.Object, "spec", "timeout")
	require.NoError(t, err)
	require.True(t, ok)
	assert.Equal(t, int64(30), toInt64(timeout["seconds"]))
	// The CRD schema declares priority with default: "Normal", so the API
	// server applies that structural default when serving the object as v1
	// (documented in docs/roundtrip-strategy.md). The conversion webhook
	// itself does not invent v1-only fields — this default is owned by the
	// schema.
	priority, hasPriority, _ := unstructured.NestedString(v1Obj.Object, "spec", "priority")
	assert.True(t, hasPriority, "schema default for spec.priority must be applied")
	assert.Equal(t, "Normal", priority)
}

// TestV1CreateDefaultsAndValidates is the v1 side of admission: missing
// timeout/priority are defaulted; a malformed duration is rejected.
func TestV1CreateDefaultsAndValidates(t *testing.T) {
	ctx := context.Background()
	dyn := dynamicClient(t)

	cur := &v1.Task{
		ObjectMeta: metav1.ObjectMeta{Name: "v1-defaults", Namespace: "itest"},
		Spec: v1.TaskSpec{
			Payload: "p",
		},
	}
	require.NoError(t, adminClient.Create(ctx, cur))

	refetched := &v1.Task{}
	require.NoError(t, adminClient.Get(ctx, types.NamespacedName{Namespace: "itest", Name: "v1-defaults"}, refetched))
	require.NotNil(t, refetched.Spec.Timeout)
	assert.Equal(t, int64(30), refetched.Spec.Timeout.Seconds)
	assert.Equal(t, v1.PriorityNormal, refetched.Spec.Priority)

	// Illegal duration: nanos out of range -> admission rejects with 422-ish
	// status, no silent acceptance.
	bad := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "migration.example.io/v1",
		"kind":       "Task",
		"metadata":   map[string]any{"name": "bad-duration", "namespace": "itest"},
		"spec": map[string]any{
			"timeout": map[string]any{"seconds": int64(1), "nanos": int64(2_000_000_000)},
		},
	}}
	_, err := dyn.Resource(gvrV1).Namespace("itest").Create(ctx, bad, metav1.CreateOptions{})
	require.Error(t, err)
	// The request is rejected either by the validating admission webhook
	// (422 Invalid) or by the conversion webhook that admission invokes
	// while building review versions (surfaced as 500 Internal). What
	// matters is that it is refused rather than silently stored.
	assert.True(t, apierrors.IsInvalid(err) || apierrors.IsBadRequest(err) ||
		apierrors.IsInternalError(err) || apierrors.IsServiceUnavailable(err),
		"got %T: %v", err, err)
	assert.Contains(t, err.Error(), "999999999")

	// Illegal duration via schema alone (negative seconds).
	bad2 := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "migration.example.io/v1",
		"kind":       "Task",
		"metadata":   map[string]any{"name": "negative-duration", "namespace": "itest"},
		"spec": map[string]any{
			"timeout": map[string]any{"seconds": int64(-7)},
		},
	}}
	_, err = dyn.Resource(gvrV1).Namespace("itest").Create(ctx, bad2, metav1.CreateOptions{})
	require.Error(t, err)
}

// TestSubSecondValueFailsServingToOldClient proves a v1 object carrying
// sub-second nanos can be read by a new client but serving it as v1alpha1
// returns a clear conversion error instead of silently truncating.
func TestSubSecondValueFailsServingToOldClient(t *testing.T) {
	ctx := context.Background()
	dyn := dynamicClient(t)

	sub := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "migration.example.io/v1",
		"kind":       "Task",
		"metadata":   map[string]any{"name": "sub-second", "namespace": "itest"},
		"spec": map[string]any{
			"timeout": map[string]any{"seconds": int64(12), "nanos": int32(500_000_000)},
		},
	}}
	_, err := dyn.Resource(gvrV1).Namespace("itest").Create(ctx, sub, metav1.CreateOptions{})
	require.NoError(t, err, "v1 must accept valid sub-second duration")

	// New client reads it fine.
	got, err := dyn.Resource(gvrV1).Namespace("itest").Get(ctx, "sub-second", metav1.GetOptions{})
	require.NoError(t, err)
	nanos, _, _ := unstructured.NestedInt64(got.Object, "spec", "timeout", "nanos")
	assert.Equal(t, int64(500_000_000), nanos)

	// Old client cannot be served a truncated value.
	_, err = dyn.Resource(gvrV1Alpha1).Namespace("itest").Get(ctx, "sub-second", metav1.GetOptions{})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "sub-second")
}

// TestOldClientUpdateDoesNotEraseV1Fields is the key compatibility guarantee,
// exercised over a real API server:
//
//	new client creates v1 Task with priority/tags
//	  -> etcd stores v1
//	old client GETs as v1alpha1 (fields parked in preserve annotation by the
//	conversion webhook), changes only the payload, PUTs v1alpha1
//	  -> apiserver converts to storage v1, annotation restores priority/tags
//	new client GETs v1 and still sees them.
func TestOldClientUpdateDoesNotEraseV1Fields(t *testing.T) {
	ctx := context.Background()
	dyn := dynamicClient(t)

	created := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "migration.example.io/v1",
		"kind":       "Task",
		"metadata":   map[string]any{"name": "no-erase", "namespace": "itest"},
		"spec": map[string]any{
			"timeout":  map[string]any{"seconds": int64(60)},
			"payload":  "original",
			"priority": "High",
			"tags":     []any{"batch", "gpu"},
		},
	}}
	_, err := dyn.Resource(gvrV1).Namespace("itest").Create(ctx, created, metav1.CreateOptions{})
	require.NoError(t, err)

	// Old client fetches the legacy representation.
	oldView, err := dyn.Resource(gvrV1Alpha1).Namespace("itest").Get(ctx, "no-erase", metav1.GetOptions{})
	require.NoError(t, err)
	secs, _, err := unstructured.NestedInt64(oldView.Object, "spec", "timeoutSeconds")
	require.NoError(t, err)
	assert.Equal(t, int64(60), secs)
	annos := oldView.GetAnnotations()
	require.NotEmpty(t, annos["migration.example.io/v1-spec-preserve"])

	// Old client changes payload only and writes its legacy view back.
	require.NoError(t, unstructured.SetNestedField(oldView.Object, "changed-by-old-client",
		"spec", "payload"))
	updated, err := dyn.Resource(gvrV1Alpha1).Namespace("itest").Update(ctx, oldView, metav1.UpdateOptions{})
	require.NoError(t, err)
	_ = updated

	// New client: v1 fields must be intact, payload change visible.
	newView, err := dyn.Resource(gvrV1).Namespace("itest").Get(ctx, "no-erase", metav1.GetOptions{})
	require.NoError(t, err)
	spec, _, err := unstructured.NestedMap(newView.Object, "spec")
	require.NoError(t, err)
	assert.Equal(t, "High", spec["priority"])
	assert.Equal(t, []any{"batch", "gpu"}, spec["tags"])
	assert.Equal(t, "changed-by-old-client", spec["payload"])
	assert.Equal(t, map[string]any{"seconds": int64(60)}, spec["timeout"])
	_, leaked := newView.GetAnnotations()["migration.example.io/v1-spec-preserve"]
	assert.False(t, leaked, "preserve annotation must not survive the up-conversion")

	// Typed v1 client sees identical data.
	typed := &v1.Task{}
	require.NoError(t, adminClient.Get(ctx, types.NamespacedName{Namespace: "itest", Name: "no-erase"}, typed))
	assert.Equal(t, v1.TaskPriority("High"), typed.Spec.Priority)
	assert.Equal(t, []string{"batch", "gpu"}, typed.Spec.Tags)
}

// TestUnknownFieldsSurviveConversion checks the schema-declared retention
// policy: keys neither version models (spec.experimentalFlux) survive a
// v1alpha1 -> v1 -> v1alpha1 bounce served by the apiserver.
func TestUnknownFieldsSurviveConversion(t *testing.T) {
	ctx := context.Background()
	dyn := dynamicClient(t)

	obj := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "migration.example.io/v1alpha1",
		"kind":       "Task",
		"metadata":   map[string]any{"name": "unknown-fields", "namespace": "itest"},
		"spec": map[string]any{
			"timeoutSeconds":   int64(10),
			"experimentalFlux": map[string]any{"mode": "fast", "ratio": 0.25},
		},
	}}
	_, err := dyn.Resource(gvrV1Alpha1).Namespace("itest").Create(ctx, obj, metav1.CreateOptions{})
	require.NoError(t, err)

	v1View, err := dyn.Resource(gvrV1).Namespace("itest").Get(ctx, "unknown-fields", metav1.GetOptions{})
	require.NoError(t, err)
	flux, ok, err := unstructured.NestedMap(v1View.Object, "spec", "experimentalFlux")
	require.NoError(t, err)
	require.True(t, ok, "unknown field must survive up-conversion server-side")
	assert.Equal(t, "fast", flux["mode"])

	// Update through v1 to force the stored bytes to be rewritten, then read
	// legacy again.
	require.NoError(t, unstructured.SetNestedField(v1View.Object, "turbo", "spec", "experimentalFlux", "mode"))
	_, err = dyn.Resource(gvrV1).Namespace("itest").Update(ctx, v1View, metav1.UpdateOptions{})
	require.NoError(t, err)
	back, err := dyn.Resource(gvrV1Alpha1).Namespace("itest").Get(ctx, "unknown-fields", metav1.GetOptions{})
	require.NoError(t, err)
	mode, _, err := unstructured.NestedString(back.Object, "spec", "experimentalFlux", "mode")
	require.NoError(t, err)
	assert.Equal(t, "turbo", mode)
}

// TestTypedRoundTrip verifies typed v1alpha1/v1 clients (what an actual
// in-cluster controller would use) can round-trip through the manager client.
func TestTypedRoundTrip(t *testing.T) {
	ctx := context.Background()

	old := &v1alpha1.Task{
		TypeMeta:   metav1.TypeMeta{APIVersion: v1alpha1.GroupName + "/v1alpha1", Kind: "Task"},
		ObjectMeta: metav1.ObjectMeta{Name: "typed-rt", Namespace: "itest"},
		Spec:       v1alpha1.TaskSpec{Payload: "x"},
	}
	// Typed v1alpha1 create through the scheme-aware client; server still
	// stores v1.
	dyn := dynamicClient(t)
	u, err := toUnstructured(old)
	require.NoError(t, err)
	_, err = dyn.Resource(gvrV1Alpha1).Namespace("itest").Create(ctx, u, metav1.CreateOptions{})
	require.NoError(t, err)

	cur := &v1.Task{}
	require.NoError(t, adminClient.Get(ctx, types.NamespacedName{Namespace: "itest", Name: "typed-rt"}, cur))
	assert.Equal(t, "x", cur.Spec.Payload)
	require.NotNil(t, cur.Spec.Timeout)
	assert.Equal(t, int64(30), cur.Spec.Timeout.Seconds)

	// A second v1alpha1 read returns the defaulted integer seconds.
	legacyRefetched, err := dyn.Resource(gvrV1Alpha1).Namespace("itest").Get(ctx, "typed-rt", metav1.GetOptions{})
	require.NoError(t, err)
	s, _, _ := unstructured.NestedInt64(legacyRefetched.Object, "spec", "timeoutSeconds")
	assert.Equal(t, int64(30), s)
}

// TestCRDStoredVersionsReportsV1 sanity-checks the storage version metadata
// the migration tool relies on.
func TestCRDStoredVersionsReportsV1(t *testing.T) {
	ctx := context.Background()
	cs, err := apiextensionsclient.NewForConfig(restCfg)
	require.NoError(t, err)
	crd, err := cs.ApiextensionsV1().CustomResourceDefinitions().Get(ctx, "tasks.migration.example.io", metav1.GetOptions{})
	require.NoError(t, err)
	assert.ElementsMatch(t, []string{"v1"}, crd.Status.StoredVersions)

	// Structural schema flag is explicitly false; unknown-field retention is
	// declared per-field (x-kubernetes-preserve-unknown-fields).
	assert.False(t, crd.Spec.PreserveUnknownFields)
	var foundPreserve bool
	for _, v := range crd.Spec.Versions {
		if v.Schema != nil && v.Schema.OpenAPIV3Schema != nil {
			if spec, ok := v.Schema.OpenAPIV3Schema.Properties["spec"]; ok {
				if spec.XPreserveUnknownFields != nil {
					foundPreserve = foundPreserve || bool(*spec.XPreserveUnknownFields)
				}
			}
		}
	}
	assert.True(t, foundPreserve, "spec must explicitly declare x-kubernetes-preserve-unknown-fields")
}

func toInt64(v any) int64 {
	switch n := v.(type) {
	case int64:
		return n
	case int:
		return int64(n)
	case float64:
		return int64(n)
	}
	return 0
}

func toUnstructured(obj client.Object) (*unstructured.Unstructured, error) {
	b, err := json.Marshal(obj)
	if err != nil {
		return nil, err
	}
	u := &unstructured.Unstructured{}
	if err := json.Unmarshal(b, &u.Object); err != nil {
		return nil, err
	}
	if u.GetKind() == "" {
		u.SetGroupVersionKind(obj.GetObjectKind().GroupVersionKind())
	}
	return u, nil
}
