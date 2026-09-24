package failinject

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	admissionv1 "k8s.io/api/admission/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"

	configv1alpha1 "github.com/example/config-distributor/api/v1alpha1"
)

func csJSON(t *testing.T, data, format string, matchLabel string) []byte {
	t.Helper()
	cs := configv1alpha1.ConfigSnapshot{
		TypeMeta: metav1.TypeMeta{APIVersion: "config.example.com/v1alpha1", Kind: "ConfigSnapshot"},
		ObjectMeta: metav1.ObjectMeta{
			Name: "snap",
		},
		Spec: configv1alpha1.ConfigSnapshotSpec{
			Payload:  configv1alpha1.Payload{Format: format, Data: data},
			Selector: metav1.LabelSelector{MatchLabels: map[string]string{"tier": matchLabel}},
		},
	}
	b, err := json.Marshal(cs)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func validateRequest(t *testing.T, op admissionv1.Operation, oldObj, newObj []byte) admissionv1.AdmissionReview {
	t.Helper()
	request := &admissionv1.AdmissionRequest{
		UID:       types.UID("v1"),
		Operation: op,
		Resource: metav1.GroupVersionResource{
			Group: "config.example.com", Version: "v1alpha1", Resource: "configsnapshots",
		},
		Object: runtimeRawExtension(newObj),
	}
	if oldObj != nil {
		request.OldObject = runtimeRawExtension(oldObj)
	}
	ar := admissionv1.AdmissionReview{
		TypeMeta: metav1.TypeMeta{APIVersion: "admission.k8s.io/v1", Kind: "AdmissionReview"},
		Request:  request,
	}
	body, err := json.Marshal(ar)
	if err != nil {
		t.Fatal(err)
	}

	srv := httptest.NewServer(http.HandlerFunc(NewWebhook(NewMatcher()).HandleValidate))
	defer srv.Close()
	return postReview(t, srv.URL, body)
}

func TestValidator_CreateRejectsEmptyPayload(t *testing.T) {
	out := validateRequest(t, admissionv1.Create, nil, csJSON(t, "", "properties", "test"))
	if out.Response.Allowed {
		t.Fatal("empty payload must be rejected")
	}
}

func TestValidator_CreateAllowsValid(t *testing.T) {
	out := validateRequest(t, admissionv1.Create, nil, csJSON(t, "k=v\n", "properties", "test"))
	if !out.Response.Allowed {
		t.Fatalf("valid create must pass: %v", out.Response.Result)
	}
}

func TestValidator_UpdateRejectsPayloadChange(t *testing.T) {
	old := csJSON(t, "k=v\n", "properties", "test")
	new := csJSON(t, "k=CHANGED\n", "properties", "test")
	out := validateRequest(t, admissionv1.Update, old, new)
	if out.Response.Allowed {
		t.Fatal("payload change must be rejected")
	}
}

func TestValidator_UpdateAllowsSelectorNarrowing(t *testing.T) {
	// Selector changes are the supported GC path and must be allowed.
	old := csJSON(t, "k=v\n", "properties", "test")
	new := csJSON(t, "k=v\n", "properties", "canary")
	out := validateRequest(t, admissionv1.Update, old, new)
	if !out.Response.Allowed {
		t.Fatalf("selector update must be allowed: %v", out.Response.Result)
	}
}
