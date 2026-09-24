// Package admission implements the mutating and validating admission
// webhooks for the Timer CRD. It works for BOTH served versions:
//
//   - mutate: v1 create without spec.priority gets priority="Normal"
//     (v1alpha1 has no such field; the default is applied on the converted
//     v1 object by the API server defaults path in real installs and is
//     mirrored here for the admission-covered path).
//   - validate: duration/priority invariants (delegated to
//     internal/validation).
package admission

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"

	v1 "github.com/example/crd-migration-demo/api/v1"
	v1alpha1 "github.com/example/crd-migration-demo/api/v1alpha1"
	"github.com/example/crd-migration-demo/internal/validation"
	"github.com/go-logr/logr"
	admissionv1 "k8s.io/api/admission/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
)

// Handler serves /mutate-v1-timer and /validate-v1-timer (path decides mode).
type Handler struct {
	Log     logr.Logger
	scheme  *runtime.Scheme
	decoder runtime.Decoder
}

// NewHandler builds the admission handler.
func NewHandler(log logr.Logger) *Handler {
	s := runtime.NewScheme()
	utilruntime.Must(v1.AddToScheme(s))
	utilruntime.Must(v1alpha1.AddToScheme(s))
	// admissionv1 types are registered via its own AddToScheme.
	utilruntime.Must(addAdmissionTypes(s))
	return &Handler{
		Log:     log,
		scheme:  s,
		decoder: admissionCodec(s),
	}
}

// ServeHTTP dispatches on the URL path: anything ending in /mutate... is a
// mutation, everything else is a validation.
func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, fmt.Sprintf("cannot read body: %v", err), http.StatusBadRequest)
		return
	}
	review := &admissionv1.AdmissionReview{}
	if _, _, err := h.decoder.Decode(body, nil, review); err != nil {
		http.Error(w, fmt.Sprintf("cannot decode AdmissionReview: %v", err), http.StatusBadRequest)
		return
	}
	if review.Request == nil {
		http.Error(w, "AdmissionReview has no request", http.StatusBadRequest)
		return
	}

	var resp *admissionv1.AdmissionResponse
	if strings.HasSuffix(r.URL.Path, "/mutate-v1-timer") {
		resp = h.mutate(review.Request)
	} else {
		resp = h.validate(review.Request)
	}

	out := &admissionv1.AdmissionReview{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "admission.k8s.io/v1",
			Kind:       "AdmissionReview",
		},
		Response: resp,
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(out)
}

func (h *Handler) mutate(req *admissionv1.AdmissionRequest) *admissionv1.AdmissionResponse {
	resp := &admissionv1.AdmissionResponse{UID: req.UID, Allowed: true}

	gv, err := parseGroupVersion(req.Kind.Version)
	if err != nil {
		return deny(req.UID, http.StatusBadRequest, err.Error())
	}

	// Only CREATE in v1 needs a priority default. Updates must never
	// overwrite an explicitly chosen (or annotation-carried) priority.
	if req.Operation != admissionv1.Create || gv != "v1" {
		return resp
	}

	var raw map[string]json.RawMessage
	if err := json.Unmarshal(req.Object.Raw, &raw); err != nil {
		return deny(req.UID, http.StatusBadRequest, fmt.Sprintf("parse object: %v", err))
	}
	specRaw, hasSpec := raw["spec"]
	if hasSpec {
		var spec map[string]json.RawMessage
		if err := json.Unmarshal(specRaw, &spec); err == nil {
			if _, hasPriority := spec["priority"]; hasPriority {
				return resp
			}
		}
	}

	patch := []map[string]string{{
		"op":    "add",
		"path":  "/spec/priority",
		"value": "Normal",
	}}
	patchBytes, _ := json.Marshal(patch)
	resp.Patch = patchBytes
	pt := admissionv1.PatchTypeJSONPatch
	resp.PatchType = &pt
	resp.AuditAnnotations = map[string]string{
		"timer.example.com/defaulted": "spec.priority=Normal",
	}
	return resp
}

func (h *Handler) validate(req *admissionv1.AdmissionRequest) *admissionv1.AdmissionResponse {
	if req.Operation == admissionv1.Delete {
		return &admissionv1.AdmissionResponse{UID: req.UID, Allowed: true}
	}

	switch req.Kind.Version {
	case "v1":
		obj := &v1.Timer{}
		if err := json.Unmarshal(req.Object.Raw, obj); err != nil {
			return deny(req.UID, http.StatusBadRequest, fmt.Sprintf("parse v1 object: %v", err))
		}
		if err := validation.ValidateV1(obj); err != nil {
			return deny(req.UID, http.StatusUnprocessableEntity, err.Error())
		}
	case "v1alpha1":
		obj := &v1alpha1.Timer{}
		if err := json.Unmarshal(req.Object.Raw, obj); err != nil {
			return deny(req.UID, http.StatusBadRequest, fmt.Sprintf("parse v1alpha1 object: %v", err))
		}
		if err := validation.ValidateV1Alpha1(obj); err != nil {
			return deny(req.UID, http.StatusUnprocessableEntity, err.Error())
		}
	default:
		return deny(req.UID, http.StatusBadRequest, fmt.Sprintf("unknown version %q", req.Kind.Version))
	}
	return &admissionv1.AdmissionResponse{UID: req.UID, Allowed: true}
}

func deny(uid typesUID, code int32, msg string) *admissionv1.AdmissionResponse {
	return &admissionv1.AdmissionResponse{
		UID:     uid,
		Allowed: false,
		Result: &metav1.Status{
			Status:  metav1.StatusFailure,
			Code:    code,
			Message: msg,
		},
	}
}
