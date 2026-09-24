package failinject

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	admissionv1 "k8s.io/api/admission/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

func reviewRaw(t *testing.T, ns, name string, op admissionv1.Operation, rawObj []byte) []byte {
	t.Helper()
	r := admissionv1.AdmissionReview{
		TypeMeta: metav1.TypeMeta{APIVersion: "admission.k8s.io/v1", Kind: "AdmissionReview"},
		Request: &admissionv1.AdmissionRequest{
			UID:       types.UID("u1"),
			Operation: op,
			Namespace: ns,
			Name:      name,
			Resource:  metav1.GroupVersionResource{Group: "", Version: "v1", Resource: "configmaps"},
			Object:    runtimeRawExtension(rawObj),
		},
	}
	b, err := json.Marshal(r)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func TestWebhook_DenyAndAllow(t *testing.T) {
	m := NewMatcher()
	m.ReplacePolicies([]FailPolicy{{
		ObjectMeta: metav1.ObjectMeta{Name: "p"},
		Spec:       FailPolicySpec{TargetNamespaces: []string{"test-b"}, Mode: ModeAlways},
	}})
	srv := httptest.NewServer(http.HandlerFunc(NewWebhook(m).HandleInject))
	defer srv.Close()

	obj := []byte(`{"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"cfg-x"}}`)

	// Denied.
	resp := postReview(t, srv.URL, reviewRaw(t, "test-b", "cfg-x", admissionv1.Create, obj))
	if resp.Response.Allowed {
		t.Fatalf("expected denial, got %+v", resp.Response)
	}
	if resp.Response.UID != types.UID("u1") {
		t.Fatal("response must echo request UID")
	}

	// Allowed in another namespace.
	resp = postReview(t, srv.URL, reviewRaw(t, "test-a", "cfg-x", admissionv1.Create, obj))
	if !resp.Response.Allowed {
		t.Fatalf("expected allow, got %+v", resp.Response)
	}
}

func postReview(t *testing.T, url string, body []byte) admissionv1.AdmissionReview {
	t.Helper()
	r, err := http.Post(url, "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer r.Body.Close()
	var out admissionv1.AdmissionReview
	if err := json.NewDecoder(r.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	return out
}
