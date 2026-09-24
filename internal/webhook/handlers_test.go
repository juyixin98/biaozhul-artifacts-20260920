package webhook

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	admissionv1 "k8s.io/api/admission/v1"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"

	"github.com/example/crd-migration-compat/internal/conversion"
)

func conversionReviewBody(t *testing.T, desired string, objs ...string) []byte {
	t.Helper()
	req := &apiextensionsv1.ConversionReview{
		TypeMeta: metav1.TypeMeta{APIVersion: "apiextensions.k8s.io/v1", Kind: "ConversionReview"},
		Request: &apiextensionsv1.ConversionRequest{
			DesiredAPIVersion: desired,
			UID:               types.UID("uid-1"),
		},
	}
	for _, o := range objs {
		req.Request.Objects = append(req.Request.Objects, runtime.RawExtension{Raw: []byte(o)})
	}
	b, err := json.Marshal(req)
	require.NoError(t, err)
	return b
}

func postJSON(t *testing.T, h http.Handler, body []byte) []byte {
	t.Helper()
	srv := httptest.NewServer(h)
	defer srv.Close()
	resp, err := http.Post(srv.URL, "application/json", bytes.NewReader(body))
	require.NoError(t, err)
	defer resp.Body.Close()
	out, err := io.ReadAll(resp.Body)
	require.NoError(t, err)
	return out
}

func TestConversionHandlerUp(t *testing.T) {
	h := NewConversionHandler(conversion.NewConverter(conversion.Hooks{}), time.Second)
	body := conversionReviewBody(t, "migration.example.io/v1", `{
		"apiVersion":"migration.example.io/v1alpha1","kind":"Task",
		"metadata":{"name":"h1","namespace":"default"},
		"spec":{"timeoutSeconds":15}}`)
	out := postJSON(t, h, body)

	review := &apiextensionsv1.ConversionReview{}
	require.NoError(t, json.Unmarshal(out, review))
	require.NotNil(t, review.Response)
	assert.Equal(t, metav1.StatusSuccess, review.Response.Result.Status,
		"unexpected failure: %s", review.Response.Result.Message)
	require.Len(t, review.Response.ConvertedObjects, 1)

	var got map[string]any
	require.NoError(t, json.Unmarshal(review.Response.ConvertedObjects[0].Raw, &got))
	assert.Equal(t, "migration.example.io/v1", got["apiVersion"])
	timeout := got["spec"].(map[string]any)["timeout"].(map[string]any)
	assert.EqualValues(t, 15, timeout["seconds"])
}

func TestConversionHandlerInexpressibleFailsBatch(t *testing.T) {
	h := NewConversionHandler(conversion.NewConverter(conversion.Hooks{}), time.Second)
	body := conversionReviewBody(t, "migration.example.io/v1alpha1",
		`{"apiVersion":"migration.example.io/v1","kind":"Task","metadata":{"name":"ok"},
			"spec":{"timeout":{"seconds":10}}}`,
		`{"apiVersion":"migration.example.io/v1","kind":"Task","metadata":{"name":"bad"},
			"spec":{"timeout":{"seconds":10,"nanos":200000000}}}`,
	)
	out := postJSON(t, h, body)

	review := &apiextensionsv1.ConversionReview{}
	require.NoError(t, json.Unmarshal(out, review))
	require.NotNil(t, review.Response)
	assert.Equal(t, metav1.StatusFailure, review.Response.Result.Status)
	assert.Contains(t, review.Response.Result.Message, "sub-second")
	assert.Contains(t, review.Response.Result.Message, "bad")
	assert.Empty(t, review.Response.ConvertedObjects)
}

func TestConversionHandlerTimeout(t *testing.T) {
	slow := conversion.NewConverter(conversion.Hooks{
		PreConvert: func(ctx context.Context, _ conversion.Direction, _ string) error {
			select {
			case <-time.After(300 * time.Millisecond):
				return nil
			case <-ctx.Done():
				return ctx.Err()
			}
		},
	})
	h := NewConversionHandler(slow, 25*time.Millisecond)
	body := conversionReviewBody(t, "migration.example.io/v1",
		`{"apiVersion":"migration.example.io/v1alpha1","kind":"Task","metadata":{"name":"slow"},
			"spec":{"timeoutSeconds":1}}`)
	start := time.Now()
	out := postJSON(t, h, body)
	elapsed := time.Since(start)

	review := &apiextensionsv1.ConversionReview{}
	require.NoError(t, json.Unmarshal(out, review))
	assert.Equal(t, metav1.StatusFailure, review.Response.Result.Status)
	assert.Contains(t, review.Response.Result.Message, "ConversionCancelled")
	assert.Less(t, elapsed, 280*time.Millisecond, "handler must abort on its own deadline")
}

func TestConversionHandlerRejectsUnsupportedDesired(t *testing.T) {
	h := NewConversionHandler(conversion.NewConverter(conversion.Hooks{}), time.Second)
	body := conversionReviewBody(t, "migration.example.io/v9", `{
		"apiVersion":"migration.example.io/v1alpha1","kind":"Task","metadata":{"name":"x"}}`)
	out := postJSON(t, h, body)
	review := &apiextensionsv1.ConversionReview{}
	require.NoError(t, json.Unmarshal(out, review))
	assert.Equal(t, metav1.StatusFailure, review.Response.Result.Status)
}

func admissionReviewBody(t *testing.T, raw string) []byte {
	t.Helper()
	r := &admissionv1.AdmissionReview{
		TypeMeta: metav1.TypeMeta{APIVersion: "admission.k8s.io/v1", Kind: "AdmissionReview"},
		Request: &admissionv1.AdmissionRequest{
			UID:       types.UID("a1"),
			Operation: admissionv1.Create,
			Object:    runtime.RawExtension{Raw: []byte(raw)},
		},
	}
	b, err := json.Marshal(r)
	require.NoError(t, err)
	return b
}

func serveAdmission(t *testing.T, admit admitFunc, body []byte) *admissionv1.AdmissionResponse {
	t.Helper()
	out := postJSON(t, &admissionHandler{admit: admit}, body)
	review := &admissionv1.AdmissionReview{}
	require.NoError(t, json.Unmarshal(out, review))
	return review.Response
}

func TestMutateV1Defaults(t *testing.T) {
	orig := `{
		"apiVersion":"migration.example.io/v1","kind":"Task",
		"metadata":{"name":"d"},"spec":{"payload":"x"}}`
	r := serveAdmission(t, mutateV1, admissionReviewBody(t, orig))
	require.True(t, r.Allowed, "defaulting must allow: %v", r.Result)
	require.NotNil(t, r.PatchType)
	assert.Equal(t, admissionv1.PatchTypeJSONPatch, *r.PatchType)

	patched := applyOps(t, orig, r.Patch)
	spec := patched["spec"].(map[string]any)
	assert.Equal(t, map[string]any{"seconds": float64(30)}, spec["timeout"])
	assert.Equal(t, "Normal", spec["priority"])
	assert.Equal(t, "x", spec["payload"])
}

func TestMutateV1KeepsExplicitValues(t *testing.T) {
	body := admissionReviewBody(t, `{
		"apiVersion":"migration.example.io/v1","kind":"Task",
		"metadata":{"name":"d"},
		"spec":{"timeout":{"seconds":90,"nanos":1},"priority":"High","tags":["a"]}}`)
	r := serveAdmission(t, mutateV1, body)
	require.True(t, r.Allowed)
	assert.Nil(t, r.Patch, "nothing to default")
}

func TestMutateV1Alpha1Defaults(t *testing.T) {
	orig := `{
		"apiVersion":"migration.example.io/v1alpha1","kind":"Task",
		"metadata":{"name":"d"},"spec":{"payload":"x"}}`
	r := serveAdmission(t, mutateV1Alpha1, admissionReviewBody(t, orig))
	require.True(t, r.Allowed)
	patched := applyOps(t, orig, r.Patch)
	spec := patched["spec"].(map[string]any)
	assert.Equal(t, float64(30), spec["timeoutSeconds"])
}

func TestValidateV1RejectsBadDuration(t *testing.T) {
	body := admissionReviewBody(t, `{
		"apiVersion":"migration.example.io/v1","kind":"Task",
		"metadata":{"name":"d"},"spec":{"timeout":{"seconds":-1,"nanos":2000000000}}}`)
	r := serveAdmission(t, validateV1, body)
	assert.False(t, r.Allowed)
	msg := r.Result.Message
	assert.Contains(t, msg, ">= 0")
	assert.Contains(t, msg, "[0,999999999]")
}

func TestValidateV1AcceptsGoodDuration(t *testing.T) {
	body := admissionReviewBody(t, `{
		"apiVersion":"migration.example.io/v1","kind":"Task",
		"metadata":{"name":"d"},
		"spec":{"timeout":{"seconds":1,"nanos":999999999},"priority":"Low","tags":["x"]}}`)
	r := serveAdmission(t, validateV1, body)
	assert.True(t, r.Allowed, "valid object rejected: %v", r.Result)
}

func TestValidateV1Alpha1RejectsNegative(t *testing.T) {
	body := admissionReviewBody(t, `{
		"apiVersion":"migration.example.io/v1alpha1","kind":"Task",
		"metadata":{"name":"d"},"spec":{"timeoutSeconds":-3}}`)
	r := serveAdmission(t, validateV1Alpha1, body)
	assert.False(t, r.Allowed)
	assert.Contains(t, r.Result.Message, ">= 0")
}

// applyOps is a minimal RFC6902 applier for the add/replace ops the
// defaulting webhook emits (nested object paths only).
func applyOps(t *testing.T, origJSON string, opsJSON []byte) map[string]any {
	t.Helper()
	var doc map[string]any
	require.NoError(t, json.Unmarshal([]byte(origJSON), &doc))
	var ops []struct {
		Op    string          `json:"op"`
		Path  string          `json:"path"`
		Value json.RawMessage `json:"value"`
	}
	require.NoError(t, json.Unmarshal(opsJSON, &ops))
	for _, op := range ops {
		var v any
		require.NoError(t, json.Unmarshal(op.Value, &v))
		segs := strings.Split(strings.TrimPrefix(op.Path, "/"), "/")
		cur := doc
		for i := 0; i < len(segs)-1; i++ {
			next, ok := cur[segs[i]].(map[string]any)
			if !ok {
				return failPatch(t, fmt.Sprintf("segment %q is not an object", segs[i]))
			}
			cur = next
		}
		cur[segs[len(segs)-1]] = v
	}
	return doc
}

func failPatch(t *testing.T, msg string) map[string]any {
	t.Helper()
	t.Fatalf("bad RFC6902 patch: %s", msg)
	return nil
}
