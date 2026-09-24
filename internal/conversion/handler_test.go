package conversion

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/go-logr/logr"
	apiextv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
)

func alphaRaw(name string, seconds int64, annotations map[string]string) []byte {
	t := map[string]any{
		"apiVersion": "timer.example.com/v1alpha1",
		"kind":       "Timer",
		"metadata":   map[string]any{"name": name, "namespace": "default", "annotations": annotations},
		"spec":       map[string]any{"intervalSeconds": seconds},
	}
	b, _ := json.Marshal(t)
	return b
}

func v1Raw(name string, seconds int64, nanos int32, annotations map[string]string) []byte {
	t := map[string]any{
		"apiVersion": "timer.example.com/v1",
		"kind":       "Timer",
		"metadata":   map[string]any{"name": name, "namespace": "default", "annotations": annotations},
		"spec":       map[string]any{"interval": map[string]any{"seconds": seconds, "nanos": nanos}},
	}
	b, _ := json.Marshal(t)
	return b
}

func review(rawObjects ...[]byte) []byte {
	objs := make([]runtime.RawExtension, 0, len(rawObjects))
	for _, r := range rawObjects {
		objs = append(objs, runtime.RawExtension{Raw: r})
	}
	req := apiextv1.ConversionReview{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "apiextensions.k8s.io/v1",
			Kind:       "ConversionReview",
		},
		Request: &apiextv1.ConversionRequest{
			UID:               types.UID("uid-1"),
			DesiredAPIVersion: "timer.example.com/v1",
			Objects:           objs,
		},
	}
	b, _ := json.Marshal(req)
	return b
}

func postReview(t *testing.T, h *Handler, body []byte) *apiextv1.ConversionReview {
	t.Helper()
	rr := httptest.NewRecorder()
	r := httptest.NewRequest(http.MethodPost, "/convert", bytes.NewReader(body))
	h.ServeHTTP(rr, r)
	if rr.Code != http.StatusOK {
		t.Fatalf("HTTP %d: %s", rr.Code, rr.Body.String())
	}
	out := &apiextv1.ConversionReview{}
	if err := json.Unmarshal(rr.Body.Bytes(), out); err != nil {
		t.Fatalf("decode response: %v\nbody=%s", err, rr.Body.String())
	}
	return out
}

func TestHandler_UpBatchSuccess(t *testing.T) {
	h := NewHandler(logr.Discard())
	out := postReview(t, h, review(alphaRaw("a", 1, nil), alphaRaw("b", 60, nil)))
	if out.Response == nil {
		t.Fatal("no response")
	}
	if out.Response.Result.Status != metav1.StatusSuccess {
		t.Fatalf("status = %s msg=%s", out.Response.Result.Status, out.Response.Result.Message)
	}
	if len(out.Response.ConvertedObjects) != 2 {
		t.Fatalf("converted %d objects, want 2", len(out.Response.ConvertedObjects))
	}
	for _, raw := range out.Response.ConvertedObjects {
		var head struct {
			APIVersion string `json:"apiVersion"`
			Spec       struct {
				Interval struct {
					Seconds int64 `json:"seconds"`
				} `json:"interval"`
			} `json:"spec"`
		}
		if err := json.Unmarshal(raw.Raw, &head); err != nil {
			t.Fatalf("decode converted object: %v", err)
		}
		if head.APIVersion != "timer.example.com/v1" {
			t.Fatalf("apiVersion = %s, want v1", head.APIVersion)
		}
		if head.Spec.Interval.Seconds == 0 {
			t.Fatalf("seconds lost: %+v", head)
		}
	}
	if got := h.ConversionsSucceeded.Load(); got != 2 {
		t.Fatalf("success counter = %d, want 2", got)
	}
}

func TestHandler_DownSubSecondFailsBatch(t *testing.T) {
	h := NewHandler(logr.Discard())
	req := apiextv1.ConversionReview{
		TypeMeta: metav1.TypeMeta{APIVersion: "apiextensions.k8s.io/v1", Kind: "ConversionReview"},
		Request: &apiextv1.ConversionRequest{
			UID:               types.UID("uid-2"),
			DesiredAPIVersion: "timer.example.com/v1alpha1",
			Objects: []runtime.RawExtension{
				{Raw: v1Raw("good", 3, 0, nil)},
				{Raw: v1Raw("bad", 0, 500_000_000, nil)},
			},
		},
	}
	b, _ := json.Marshal(req)
	out := postReview(t, h, b)
	if out.Response.Result.Status != metav1.StatusFailure {
		t.Fatalf("expected failure, got %s: %s", out.Response.Result.Status, out.Response.Result.Message)
	}
	if !strings.Contains(out.Response.Result.Message, "cannot be expressed in v1alpha1") {
		t.Fatalf("failure should explain representability, got: %s", out.Response.Result.Message)
	}
	if len(out.Response.ConvertedObjects) != 0 {
		t.Fatalf("failed batch must return no converted objects, got %d", len(out.Response.ConvertedObjects))
	}
}

func TestHandler_BadDesiredVersion(t *testing.T) {
	h := NewHandler(logr.Discard())
	b := review(alphaRaw("a", 1, nil))
	// Force an unsupported desired version.
	var req apiextv1.ConversionReview
	_ = json.Unmarshal(b, &req)
	req.Request.DesiredAPIVersion = "timer.example.com/v9"
	b2, _ := json.Marshal(req)
	out := postReview(t, h, b2)
	if out.Response.Result.Status != metav1.StatusFailure {
		t.Fatalf("expected failure for bad desired version, got: %+v", out.Response.Result)
	}
}

// TestHandler_ContextDeadlineDuringInjectedDelay exercises the conversion
// timeout requirement at the handler contract: a request whose context
// expires while the webhook is blocked must return a context error rather
// than wait out the full delay.
func TestHandler_ContextDeadlineDuringInjectedDelay(t *testing.T) {
	h := NewHandler(logr.Discard())
	body := review(alphaRaw("slow", 1, map[string]string{
		InjectDelayAnnotation: "5s",
	}))

	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()

	r := httptest.NewRequest(http.MethodPost, "/convert", bytes.NewReader(body)).WithContext(ctx)
	rr := httptest.NewRecorder()
	start := time.Now()
	h.ServeHTTP(rr, r)
	elapsed := time.Since(start)

	if elapsed > 2*time.Second {
		t.Fatalf("handler ignored context deadline, elapsed=%s", elapsed)
	}
	out := &apiextv1.ConversionReview{}
	if err := json.Unmarshal(rr.Body.Bytes(), out); err != nil {
		t.Fatalf("decode: %v (%s)", err, rr.Body.String())
	}
	if out.Response == nil || out.Response.Result.Status != metav1.StatusFailure {
		t.Fatalf("expected failure after context deadline, got %+v", out.Response)
	}
	if !strings.Contains(out.Response.Result.Message, "context") {
		t.Fatalf("expected context-related message, got: %s", out.Response.Result.Message)
	}
}

func TestDelayForObject(t *testing.T) {
	if d := DelayForObject(alphaRaw("a", 1, nil)); d != 0 {
		t.Fatalf("no annotation -> 0, got %s", d)
	}
	d := DelayForObject(alphaRaw("a", 1, map[string]string{InjectDelayAnnotation: "150ms"}))
	if d != 150*time.Millisecond {
		t.Fatalf("delay = %s, want 150ms", d)
	}
	// Garbage values are ignored, never panic.
	if d := DelayForObject(alphaRaw("a", 1, map[string]string{InjectDelayAnnotation: "not-a-duration"})); d != 0 {
		t.Fatalf("garbage delay should be 0, got %s", d)
	}
}
