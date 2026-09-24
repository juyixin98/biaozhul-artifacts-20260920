package failinject

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"

	admissionv1 "k8s.io/api/admission/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/klog/v2"

	configv1alpha1 "github.com/example/config-distributor/api/v1alpha1"
)

// HandleValidate serves the /validate-configsnapshot admission endpoint.
func (w *Webhook) HandleValidate(rw http.ResponseWriter, req *http.Request) {
	if req.Method != http.MethodPost {
		http.Error(rw, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	body, err := io.ReadAll(io.LimitReader(req.Body, 8<<20))
	if err != nil {
		http.Error(rw, err.Error(), http.StatusBadRequest)
		return
	}
	review := admissionv1.AdmissionReview{}
	if _, _, err := Codecs.UniversalDeserializer().Decode(body, nil, &review); err != nil {
		http.Error(rw, fmt.Sprintf("decode: %v", err), http.StatusBadRequest)
		return
	}
	if review.Request == nil {
		http.Error(rw, "missing request", http.StatusBadRequest)
		return
	}
	r := review.Request

	allowed := true
	var msg string
	switch r.Operation {
	case admissionv1.Create:
		var cs configv1alpha1.ConfigSnapshot
		if err := json.Unmarshal(r.Object.Raw, &cs); err != nil {
			allowed, msg = false, fmt.Sprintf("decode ConfigSnapshot: %v", err)
		} else if cs.Spec.Payload.Data == "" {
			allowed, msg = false, "spec.payload.data must be non-empty"
		}
	case admissionv1.Update:
		var oldCS, newCS configv1alpha1.ConfigSnapshot
		if err := json.Unmarshal(r.OldObject.Raw, &oldCS); err != nil {
			allowed, msg = false, fmt.Sprintf("decode old object: %v", err)
		} else if err := json.Unmarshal(r.Object.Raw, &newCS); err != nil {
			allowed, msg = false, fmt.Sprintf("decode new object: %v", err)
		} else if reason := immutableViolation(&oldCS, &newCS); reason != "" {
			allowed, msg = false, reason
		}
	default:
		// DELETE/CONNECT: nothing to validate.
	}

	resp := &admissionv1.AdmissionResponse{UID: r.UID, Allowed: allowed}
	if !allowed {
		resp.Result = &metav1.Status{
			Status:  "Failure",
			Message: msg,
			Reason:  metav1.StatusReasonForbidden,
			Code:    http.StatusForbidden,
		}
		klog.InfoS("rejected ConfigSnapshot", "operation", r.Operation, "message", msg)
	}
	out := admissionv1.AdmissionReview{
		TypeMeta: metav1.TypeMeta{
			APIVersion: admissionv1.SchemeGroupVersion.String(),
			Kind:       "AdmissionReview",
		},
		Response: resp,
	}
	rw.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(rw).Encode(&out)
}

// immutableViolation returns a human-readable reason when an update changes
// payload data/format or the selector; metadata/spec-adjacent edits pass.
func immutableViolation(old, new *configv1alpha1.ConfigSnapshot) string {
	if old.Spec.Payload.Data != new.Spec.Payload.Data {
		return "spec.payload.data is immutable; create a new ConfigSnapshot for new content"
	}
	om, _ := json.Marshal(old.Spec.Payload)
	nm, _ := json.Marshal(new.Spec.Payload)
	if string(om) != string(nm) {
		return "spec.payload is immutable; create a new ConfigSnapshot for new content"
	}
	return ""
}
