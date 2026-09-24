package webhook

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"

	"github.com/example/crd-migration-compat/internal/conversion"
)

// ConversionHandler serves POST /convert with apiextensions.k8s.io/v1
// ConversionReview objects.
type ConversionHandler struct {
	Converter *conversion.Converter
	// Timeout bounds a single object conversion. Zero means no additional
	// deadline beyond the HTTP request's context.
	Timeout time.Duration
}

// NewConversionHandler builds the handler with the given per-request budget
// (kube-apiserver itself calls webhooks with a ~10s/30s budget depending on
// failurePolicy/timeoutSeconds configuration).
func NewConversionHandler(c *conversion.Converter, timeout time.Duration) *ConversionHandler {
	if timeout == 0 {
		timeout = 10 * time.Second
	}
	return &ConversionHandler{Converter: c, Timeout: timeout}
}

var _ http.Handler = (*ConversionHandler)(nil)

// objectFailure keeps per-object detail for the aggregate failure message.
type objectFailure struct {
	index  int
	name   string
	code   int32
	reason metav1.StatusReason
	err    error
}

// Handle implements http.Handler.
func (h *ConversionHandler) Handle(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 6*1024*1024))
	if err != nil {
		http.Error(w, fmt.Sprintf("read body: %v", err), http.StatusBadRequest)
		return
	}
	if len(body) == 0 {
		http.Error(w, "empty body", http.StatusBadRequest)
		return
	}

	reqReview := &apiextensionsv1.ConversionReview{}
	if _, _, err := universalDecoder.Decode(body, nil, reqReview); err != nil {
		http.Error(w, fmt.Sprintf("decode ConversionReview: %v", err), http.StatusBadRequest)
		return
	}
	if reqReview.Request == nil {
		http.Error(w, "ConversionReview.request missing", http.StatusBadRequest)
		return
	}

	targetGV, err := schema.ParseGroupVersion(reqReview.Request.DesiredAPIVersion)
	if err != nil || targetGV.Group != groupName ||
		(targetGV.Version != conversion.V1 && targetGV.Version != conversion.V1Alpha1) {
		failReview(w, reqReview, http.StatusUnprocessableEntity, metav1.StatusReasonInvalid,
			fmt.Sprintf("only %s v1alpha1/v1 are served, desired %q", groupName, reqReview.Request.DesiredAPIVersion))
		return
	}

	converted := make([]runtime.RawExtension, 0, len(reqReview.Request.Objects))
	var failures []objectFailure

	for i, raw := range reqReview.Request.Objects {
		obj := &unstructured.Unstructured{}
		if err := json.Unmarshal(raw.Raw, &obj.Object); err != nil {
			failures = append(failures, objectFailure{
				index: i, code: http.StatusBadRequest, reason: metav1.StatusReasonBadRequest,
				err: fmt.Errorf("cannot decode input object: %w", err),
			})
			continue
		}
		gvk := obj.GroupVersionKind()
		if gvk.Group != groupName {
			failures = append(failures, objectFailure{
				index: i, name: obj.GetName(), code: http.StatusBadRequest, reason: metav1.StatusReasonBadRequest,
				err: fmt.Errorf("unexpected group %q", gvk.Group),
			})
			continue
		}

		ctx := r.Context()
		var cancel context.CancelFunc
		if h.Timeout > 0 {
			ctx, cancel = context.WithTimeout(r.Context(), h.Timeout)
		}
		out, cerr := h.Converter.ConvertObject(ctx, obj, gvk.Version, targetGV.Version)
		if cancel != nil {
			cancel()
		}
		if cerr != nil {
			code, reason := mapConversionError(cerr)
			failures = append(failures, objectFailure{index: i, name: obj.GetName(), code: code, reason: reason, err: cerr})
			continue
		}
		outRaw, merr := json.Marshal(out)
		if merr != nil {
			failures = append(failures, objectFailure{
				index: i, name: obj.GetName(), code: http.StatusInternalServerError,
				reason: metav1.StatusReasonInternalError,
				err:    fmt.Errorf("cannot re-encode converted object: %w", merr),
			})
			continue
		}
		converted = append(converted, runtime.RawExtension{Raw: outRaw})
	}

	if len(failures) > 0 {
		failReview(w, reqReview, http.StatusUnprocessableEntity, metav1.StatusReasonInvalid,
			summarizeFailures(failures))
		return
	}

	writeJSON(w, http.StatusOK, &apiextensionsv1.ConversionReview{
		TypeMeta: metav1.TypeMeta{APIVersion: "apiextensions.k8s.io/v1", Kind: "ConversionReview"},
		Response: &apiextensionsv1.ConversionResponse{
			UID:              reqReview.Request.UID,
			ConvertedObjects: converted,
			Result:           metav1.Status{Status: metav1.StatusSuccess},
		},
	})
}

// ServeHTTP allows passing the handler to http muxes directly.
func (h *ConversionHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	h.Handle(w, r)
}

func mapConversionError(err error) (int32, metav1.StatusReason) {
	var ce *conversion.ConversionError
	if errors.As(err, &ce) {
		switch ce.Reason {
		case conversion.ReasonInexpressibleValue, conversion.ReasonInvalidField, conversion.ReasonMalformedPreserve:
			// 422 semantics: the request is well-formed but the data cannot be
			// converted. Surfaced to the caller as a failed conversion.
			return http.StatusUnprocessableEntity, metav1.StatusReasonInvalid
		case conversion.ReasonContextDone:
			return http.StatusGatewayTimeout, metav1.StatusReasonTimeout
		}
	}
	return http.StatusInternalServerError, metav1.StatusReasonInternalError
}

func summarizeFailures(fs []objectFailure) string {
	var b strings.Builder
	b.WriteString("conversion failed for one or more objects:")
	for _, f := range fs {
		label := f.name
		if label == "" {
			label = fmt.Sprintf("object[%d]", f.index)
		}
		fmt.Fprintf(&b, "\n  [%d] %s: %v", f.index, label, f.err)
	}
	return b.String()
}

func failReview(w http.ResponseWriter, reqReview *apiextensionsv1.ConversionReview, code int32, reason metav1.StatusReason, msg string) {
	writeJSON(w, http.StatusOK, &apiextensionsv1.ConversionReview{
		TypeMeta: metav1.TypeMeta{APIVersion: "apiextensions.k8s.io/v1", Kind: "ConversionReview"},
		Response: &apiextensionsv1.ConversionResponse{
			UID: reqReview.Request.UID,
			Result: metav1.Status{
				Status: metav1.StatusFailure, Code: code, Reason: reason, Message: msg,
			},
		},
	})
}
