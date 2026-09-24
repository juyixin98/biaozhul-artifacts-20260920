package webhook

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"

	jsonpatch "github.com/evanphx/json-patch/v5"
	admissionv1 "k8s.io/api/admission/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const (
	pathMutateV1       = "/mutate-migration-example-io-v1-task"
	pathMutateV1Alpha1 = "/mutate-migration-example-io-v1alpha1-task"
	pathValidateV1     = "/validate-migration-example-io-v1-task"
	pathValidateV1A1   = "/validate-migration-example-io-v1alpha1-task"
)

// admitFunc performs admission on a single object given as a decoded raw JSON
// tree. It returns a JSON merge patch (RFC 7386) to apply and a list of
// human-readable validation failures.
//
// Processing raw maps + returning a sparse merge patch means the webhook never
// re-serializes the whole object through a Go struct and therefore cannot
// prune a field it does not know about.
type admitFunc func(operation admissionv1.Operation, raw map[string]any) (mergePatch map[string]any, failures []string)

// admissionHandler is a generic admission.k8s.io/v1 endpoint bound to one
// admitFunc.
type admissionHandler struct {
	admit admitFunc
}

func (h *admissionHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 6*1024*1024))
	if err != nil {
		http.Error(w, fmt.Sprintf("read body: %v", err), http.StatusBadRequest)
		return
	}

	review := &admissionv1.AdmissionReview{}
	if _, _, err := admissionDecoder.Decode(body, nil, review); err != nil {
		http.Error(w, fmt.Sprintf("decode AdmissionReview: %v", err), http.StatusBadRequest)
		return
	}
	if review.Request == nil {
		http.Error(w, "AdmissionReview.request missing", http.StatusBadRequest)
		return
	}

	resp := &admissionv1.AdmissionResponse{UID: review.Request.UID, Allowed: true}

	var rawObj map[string]any
	if len(review.Request.Object.Raw) > 0 {
		if err := json.Unmarshal(review.Request.Object.Raw, &rawObj); err != nil {
			deny(resp, http.StatusBadRequest, metav1.StatusReasonBadRequest,
				fmt.Sprintf("cannot decode admitted object: %v", err))
			writeAdmissionResponse(w, review, resp)
			return
		}
	}

	patchDoc, failures := h.admit(review.Request.Operation, rawObj)
	if len(failures) > 0 {
		deny(resp, http.StatusUnprocessableEntity, metav1.StatusReasonInvalid,
			"Task failed validation:\n  - "+strings.Join(failures, "\n  - "))
		writeAdmissionResponse(w, review, resp)
		return
	}

	if len(patchDoc) > 0 {
		original := review.Request.Object.Raw
		if len(original) == 0 {
			original = []byte("{}")
		}
		patchBytes, err := json.Marshal(patchDoc)
		if err != nil {
			deny(resp, http.StatusInternalServerError, metav1.StatusReasonInternalError, err.Error())
			writeAdmissionResponse(w, review, resp)
			return
		}
		modified, err := jsonpatch.MergePatch(original, patchBytes)
		if err != nil {
			deny(resp, http.StatusInternalServerError, metav1.StatusReasonInternalError,
				fmt.Sprintf("apply defaulting patch: %v", err))
			writeAdmissionResponse(w, review, resp)
			return
		}
		// AdmissionReview requires an RFC 6902 JSON Patch; derive the
		// add/replace operations from the diff between the two documents.
		opsBytes := jsonMergeTo6902(original, modified)
		if _, err := jsonpatch.DecodePatch(opsBytes); err != nil {
			deny(resp, http.StatusInternalServerError, metav1.StatusReasonInternalError,
				fmt.Sprintf("encode json patch: %v", err))
			writeAdmissionResponse(w, review, resp)
			return
		}
		pt := admissionv1.PatchTypeJSONPatch
		resp.PatchType = &pt
		resp.Patch = opsBytes
	}

	writeAdmissionResponse(w, review, resp)
}

func deny(resp *admissionv1.AdmissionResponse, code int32, reason metav1.StatusReason, msg string) {
	resp.Allowed = false
	resp.Result = &metav1.Status{
		Status: metav1.StatusFailure, Code: code, Reason: reason, Message: msg,
	}
}

func writeAdmissionResponse(w http.ResponseWriter, review *admissionv1.AdmissionReview, resp *admissionv1.AdmissionResponse) {
	writeJSON(w, http.StatusOK, &admissionv1.AdmissionReview{
		TypeMeta: metav1.TypeMeta{APIVersion: "admission.k8s.io/v1", Kind: "AdmissionReview"},
		Response: resp,
	})
}
