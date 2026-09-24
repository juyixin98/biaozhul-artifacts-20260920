package conversion

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync/atomic"
	"time"

	v1 "github.com/example/crd-migration-demo/api/v1"
	v1alpha1 "github.com/example/crd-migration-demo/api/v1alpha1"
	"github.com/go-logr/logr"
	apiextv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	apiextscheme "k8s.io/apiextensions-apiserver/pkg/client/clientset/clientset/scheme"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/runtime/serializer"
	"k8s.io/apimachinery/pkg/types"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
)

// Handler is the /convert endpoint. It is a plain http.Handler rather than a
// controller-runtime admission.Handler because ConversionReview is not an
// AdmissionReview.
type Handler struct {
	Log logr.Logger

	// Stats counters, readable from tests.
	ConversionsSucceeded atomic.Int64
	ConversionsFailed    atomic.Int64

	scheme  *runtime.Scheme
	decoder runtime.Decoder
}

// NewHandler builds a Handler with a scheme that knows both Timer versions
// and the apiextensions ConversionReview types.
func NewHandler(log logr.Logger) *Handler {
	s := runtime.NewScheme()
	utilruntime.Must(v1.AddToScheme(s))
	utilruntime.Must(v1alpha1.AddToScheme(s))
	utilruntime.Must(apiextscheme.AddToScheme(s))
	return &Handler{
		Log:     log,
		scheme:  s,
		decoder: serializer.NewCodecFactory(s).UniversalDeserializer(),
	}
}

// Scheme exposes the typed scheme (useful for migrator/tests sharing it).
func (h *Handler) Scheme() *runtime.Scheme { return h.scheme }

// ResetStats zeroes the conversion counters.
func (h *Handler) ResetStats() {
	h.ConversionsSucceeded.Store(0)
	h.ConversionsFailed.Store(0)
}

// ServeHTTP implements http.Handler.
func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, fmt.Sprintf("cannot read body: %v", err), http.StatusBadRequest)
		return
	}

	obj, gvk, err := h.decoder.Decode(body, nil, nil)
	if err != nil {
		http.Error(w, fmt.Sprintf("cannot decode ConversionReview: %v", err), http.StatusBadRequest)
		return
	}
	if gvk.Group != "apiextensions.k8s.io" || gvk.Kind != "ConversionReview" {
		http.Error(w, fmt.Sprintf("unsupported review type: %s", gvk), http.StatusBadRequest)
		return
	}

	req, ok := obj.(*apiextv1.ConversionReview)
	if !ok {
		http.Error(w, "decoded object is not a ConversionReview", http.StatusInternalServerError)
		return
	}
	if req.Request == nil {
		http.Error(w, "ConversionReview has no request", http.StatusBadRequest)
		return
	}

	resp := &apiextv1.ConversionReview{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "apiextensions.k8s.io/v1",
			Kind:       "ConversionReview",
		},
		Response: h.convertList(r.Context(), req.Request),
	}

	w.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(w).Encode(resp); err != nil {
		h.Log.Error(err, "failed to encode conversion response")
	}
}

// convertList converts every object to requestedAPIVersion. Conversion is
// all-or-nothing per request: one unrepresentable object fails the whole
// batch, matching how the API server treats a failed conversion.
func (h *Handler) convertList(ctx context.Context, request *apiextv1.ConversionRequest) *apiextv1.ConversionResponse {
	targetGV, err := parseAPIVersion(request.DesiredAPIVersion)
	if err != nil {
		h.ConversionsFailed.Add(1)
		return failedConversion(request.UID, http.StatusBadRequest, err.Error())
	}

	converted := make([]runtime.RawExtension, 0, len(request.Objects))
	for i := range request.Objects {
		out, err := h.convertOne(ctx, request.Objects[i].Raw, targetGV)
		if err != nil {
			h.ConversionsFailed.Add(1)
			h.Log.Error(err, "conversion failed", "desiredAPIVersion", request.DesiredAPIVersion)
			return failedConversion(request.UID, http.StatusUnprocessableEntity, err.Error())
		}
		converted = append(converted, runtime.RawExtension{Raw: out})
		h.ConversionsSucceeded.Add(1)
	}

	return &apiextv1.ConversionResponse{
		UID:              request.UID,
		Result:           metav1.Status{Status: metav1.StatusSuccess},
		ConvertedObjects: converted,
	}
}

func (h *Handler) convertOne(ctx context.Context, raw []byte, target schema.GroupVersion) ([]byte, error) {
	// Ops/test hook: per-object injected delay, resolved before any work so
	// the apiserver-side deadline (the request context) genuinely expires
	// while we are blocked.
	if delay := DelayForObject(raw); delay > 0 {
		h.Log.Info("injecting conversion delay", "delay", delay.String())
		select {
		case <-time.After(delay):
		case <-ctx.Done():
			return nil, fmt.Errorf("conversion cancelled while sleeping (%s): %w", delay, ctx.Err())
		}
	}
	if err := ctx.Err(); err != nil {
		return nil, fmt.Errorf("conversion context done before work: %w", err)
	}

	obj, gvk, err := h.decoder.Decode(raw, nil, nil)
	if err != nil {
		return nil, fmt.Errorf("decoding source object: %w", err)
	}

	switch {
	case *gvk == v1alpha1.GroupVersion.WithKind("Timer"):
		src := obj.(*v1alpha1.Timer)
		if target == v1.GroupVersion {
			dst := &v1.Timer{}
			if err := ConvertV1Alpha1ToV1(src, dst); err != nil {
				return nil, err
			}
			return marshalTimer(v1.GroupVersion, dst)
		}
		if target == v1alpha1.GroupVersion {
			return marshalTimer(v1alpha1.GroupVersion, src)
		}
		return nil, fmt.Errorf("unsupported target version: %s", target)

	case *gvk == v1.GroupVersion.WithKind("Timer"):
		src := obj.(*v1.Timer)
		if target == v1alpha1.GroupVersion {
			dst := &v1alpha1.Timer{}
			if err := ConvertV1ToV1Alpha1(src, dst); err != nil {
				return nil, err
			}
			return marshalTimer(v1alpha1.GroupVersion, dst)
		}
		if target == v1.GroupVersion {
			return marshalTimer(v1.GroupVersion, src)
		}
		return nil, fmt.Errorf("unsupported target version: %s", target)

	default:
		return nil, fmt.Errorf("unsupported source object: %s", gvk)
	}
}

// marshalTimer JSON-encodes a Timer with apiVersion/kind set explicitly.
func marshalTimer(gv schema.GroupVersion, obj runtime.Object) ([]byte, error) {
	obj.GetObjectKind().SetGroupVersionKind(gv.WithKind("Timer"))
	return json.Marshal(obj)
}

// DelayForObject extracts the inject-delay annotation without requiring a
// typed decode (the raw object may be either version).
func DelayForObject(raw []byte) time.Duration {
	var head struct {
		Metadata struct {
			Annotations map[string]string `json:"annotations"`
		} `json:"metadata"`
	}
	if err := json.Unmarshal(raw, &head); err != nil {
		return 0
	}
	v, ok := head.Metadata.Annotations[InjectDelayAnnotation]
	if !ok || v == "" {
		return 0
	}
	d, err := time.ParseDuration(v)
	if err != nil || d < 0 {
		return 0
	}
	if d > 2*time.Minute {
		d = 2 * time.Minute
	}
	return d
}

func parseAPIVersion(apiVersion string) (schema.GroupVersion, error) {
	gv, err := schema.ParseGroupVersion(apiVersion)
	if err != nil {
		return schema.GroupVersion{}, fmt.Errorf("invalid desiredAPIVersion %q: %w", apiVersion, err)
	}
	if gv.Group != "timer.example.com" {
		return schema.GroupVersion{}, fmt.Errorf("unsupported group in desiredAPIVersion %q", apiVersion)
	}
	switch gv.Version {
	case "v1", "v1alpha1":
		return gv, nil
	default:
		return schema.GroupVersion{}, fmt.Errorf("unsupported desired version %q", gv.Version)
	}
}

func failedConversion(uid types.UID, code int32, msg string) *apiextv1.ConversionResponse {
	return &apiextv1.ConversionResponse{
		UID: uid,
		Result: metav1.Status{
			Status:  metav1.StatusFailure,
			Message: strings.TrimSpace(msg),
			Code:    code,
		},
	}
}
