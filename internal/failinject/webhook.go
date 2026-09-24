package failinject

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"

	admissionv1 "k8s.io/api/admission/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/klog/v2"
)

// Webhook serves the /inject endpoint implementing the Kubernetes admission
// webhook protocol (AdmissionReview v1).
type Webhook struct {
	Matcher *Matcher
}

// NewWebhook returns a Webhook backed by the given matcher.
func NewWebhook(m *Matcher) *Webhook {
	return &Webhook{Matcher: m}
}

// HandleInject serves the /inject admission endpoint.
func (w *Webhook) HandleInject(rw http.ResponseWriter, req *http.Request) {
	if req.Method != http.MethodPost {
		http.Error(rw, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	body, err := io.ReadAll(io.LimitReader(req.Body, 8<<20))
	if err != nil {
		http.Error(rw, fmt.Sprintf("read body: %v", err), http.StatusBadRequest)
		return
	}
	review := admissionv1.AdmissionReview{}
	if _, _, err := Codecs.UniversalDeserializer().Decode(body, nil, &review); err != nil {
		klog.ErrorS(err, "failed to decode admission review")
		http.Error(rw, fmt.Sprintf("decode: %v", err), http.StatusBadRequest)
		return
	}
	if review.Request == nil {
		http.Error(rw, "missing request", http.StatusBadRequest)
		return
	}

	decision := w.Matcher.Evaluate(review.Request)

	resp := &admissionv1.AdmissionResponse{
		UID:     review.Request.UID,
		Allowed: !decision.Deny,
	}
	if decision.Deny {
		resp.Result = &metav1.Status{
			Status:  "Failure",
			Message: decision.Message,
			Reason:  metav1.StatusReasonForbidden,
			Code:    http.StatusForbidden,
		}
		klog.InfoS("injected denial",
			"policy", decision.Policy,
			"namespace", review.Request.Namespace,
			"name", review.Request.Name,
			"operation", review.Request.Operation)
	}
	out := admissionv1.AdmissionReview{
		TypeMeta: metav1.TypeMeta{
			APIVersion: admissionv1.SchemeGroupVersion.String(),
			Kind:       "AdmissionReview",
		},
		Response: resp,
	}
	rw.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(rw).Encode(&out); err != nil {
		klog.ErrorS(err, "failed to encode admission response")
	}
}
