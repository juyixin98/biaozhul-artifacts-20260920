package admission

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/go-logr/logr"
	admissionv1 "k8s.io/api/admission/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
)

func reviewRaw(t *testing.T, version string, op admissionv1.Operation, object map[string]any) []byte {
	t.Helper()
	object["kind"] = "Timer"
	object["apiVersion"] = "timer.example.com/" + version
	objBytes, err := json.Marshal(object)
	if err != nil {
		t.Fatalf("marshal object: %v", err)
	}
	r := admissionv1.AdmissionReview{
		TypeMeta: metav1.TypeMeta{APIVersion: "admission.k8s.io/v1", Kind: "AdmissionReview"},
		Request: &admissionv1.AdmissionRequest{
			UID:       types.UID("u1"),
			Operation: op,
			Kind: metav1.GroupVersionKind{
				Group: "timer.example.com", Version: version, Kind: "Timer",
			},
			Object: runtime.RawExtension{Raw: objBytes},
		},
	}
	b, _ := json.Marshal(r)
	return b
}

func do(t *testing.T, h http.Handler, path string, body []byte) *admissionv1.AdmissionReview {
	t.Helper()
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest(http.MethodPost, path, bytes.NewReader(body)))
	if rr.Code != http.StatusOK {
		t.Fatalf("HTTP %d: %s", rr.Code, rr.Body.String())
	}
	out := &admissionv1.AdmissionReview{}
	if err := json.Unmarshal(rr.Body.Bytes(), out); err != nil {
		t.Fatalf("decode: %v (%s)", err, rr.Body.String())
	}
	return out
}

func TestMutate_V1CreateDefaultsPriority(t *testing.T) {
	h := NewHandler(logr.Discard())
	body := reviewRaw(t, "v1", admissionv1.Create, map[string]any{
		"metadata": map[string]any{"name": "x"},
		"spec":     map[string]any{"interval": map[string]any{"seconds": 1}},
	})
	out := do(t, h, "/mutate-v1-timer", body)
	if !out.Response.Allowed {
		t.Fatalf("expected allow, got %s", out.Response.Result.Message)
	}
	if out.Response.PatchType == nil || *out.Response.PatchType != admissionv1.PatchTypeJSONPatch {
		t.Fatalf("expected JSONPatch, got %v", out.Response.PatchType)
	}
	if !strings.Contains(string(out.Response.Patch), `"value":"Normal"`) {
		t.Fatalf("patch should default priority Normal: %s", out.Response.Patch)
	}
}

func TestMutate_V1CreateKeepsExplicitPriority(t *testing.T) {
	h := NewHandler(logr.Discard())
	body := reviewRaw(t, "v1", admissionv1.Create, map[string]any{
		"metadata": map[string]any{"name": "x"},
		"spec": map[string]any{
			"interval": map[string]any{"seconds": 1}, "priority": "High",
		},
	})
	out := do(t, h, "/mutate-v1-timer", body)
	if !out.Response.Allowed {
		t.Fatalf("expected allow: %s", out.Response.Result.Message)
	}
	if len(out.Response.Patch) != 0 {
		t.Fatalf("explicit priority must not be patched: %s", out.Response.Patch)
	}
}

func TestMutate_AlphaCreateNoPatch(t *testing.T) {
	h := NewHandler(logr.Discard())
	body := reviewRaw(t, "v1alpha1", admissionv1.Create, map[string]any{
		"metadata": map[string]any{"name": "x"},
		"spec":     map[string]any{"intervalSeconds": 1},
	})
	out := do(t, h, "/mutate-v1-timer", body)
	if !out.Response.Allowed {
		t.Fatalf("expected allow: %s", out.Response.Result.Message)
	}
	if len(out.Response.Patch) != 0 {
		t.Fatalf("v1alpha1 create should not be patched: %s", out.Response.Patch)
	}
}

func TestValidate_RejectsBadDurations(t *testing.T) {
	h := NewHandler(logr.Discard())
	body := reviewRaw(t, "v1", admissionv1.Create, map[string]any{
		"metadata": map[string]any{"name": "x"},
		"spec": map[string]any{
			"interval": map[string]any{"seconds": 1, "nanos": -1}, // sign mismatch
		},
	})
	out := do(t, h, "/validate-v1-timer", body)
	if out.Response.Allowed {
		t.Fatal("sign-mismatched duration must be denied")
	}
	if !strings.Contains(out.Response.Result.Message, "same sign") {
		t.Fatalf("unexpected denial reason: %s", out.Response.Result.Message)
	}
}

func TestValidate_AllowsGoodV1Alpha1(t *testing.T) {
	h := NewHandler(logr.Discard())
	body := reviewRaw(t, "v1alpha1", admissionv1.Create, map[string]any{
		"metadata": map[string]any{"name": "x"},
		"spec":     map[string]any{"intervalSeconds": 5, "timeoutSeconds": 1},
	})
	out := do(t, h, "/validate-v1-timer", body)
	if !out.Response.Allowed {
		t.Fatalf("good v1alpha1 object denied: %s", out.Response.Result.Message)
	}
}
