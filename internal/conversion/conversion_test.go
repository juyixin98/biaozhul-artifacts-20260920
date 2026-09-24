package conversion

import (
	"encoding/json"
	"strings"
	"testing"

	v1 "github.com/example/crd-migration-demo/api/v1"
	v1alpha1 "github.com/example/crd-migration-demo/api/v1alpha1"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
)

func int64Ptr(i int64) *int64 { return &i }

func jsonRaw(t *testing.T, b []byte) apiextensionsv1.JSON {
	t.Helper()
	var v apiextensionsv1.JSON
	if err := json.Unmarshal(b, &v); err != nil {
		t.Fatalf("unmarshal raw json %q: %v", string(b), err)
	}
	return v
}

func TestConvertUp_WholeSeconds(t *testing.T) {
	src := &v1alpha1.Timer{
		Spec: v1alpha1.TimerSpec{
			IntervalSeconds: 30,
			TimeoutSeconds:  int64Ptr(5),
			Extra: map[string]apiextensionsv1.JSON{
				"region": jsonRaw(t, []byte(`"cn-north-1"`)),
				"nested": jsonRaw(t, []byte(`{"a":[1,2,3],"b":true}`)),
			},
		},
	}
	dst := &v1.Timer{}
	if err := ConvertV1Alpha1ToV1(src, dst); err != nil {
		t.Fatalf("ConvertV1Alpha1ToV1: %v", err)
	}
	if dst.Spec.Interval.Seconds != 30 || dst.Spec.Interval.Nanos != 0 {
		t.Fatalf("interval = %+v, want {30 0}", dst.Spec.Interval)
	}
	if dst.Spec.Timeout == nil || dst.Spec.Timeout.Seconds != 5 {
		t.Fatalf("timeout = %+v, want seconds 5", dst.Spec.Timeout)
	}
	if got := dst.Spec.Extra["region"].Raw; string(got) != `"cn-north-1"` {
		t.Fatalf("extra.region = %s", got)
	}
	if _, ok := dst.Spec.Extra["nested"]; !ok {
		t.Fatalf("extra.nested lost during conversion")
	}
}

// TestRoundTrip_AlphaToV1ToAlpha proves whole-second values survive a full
// hub round trip byte-semantically at the field level.
func TestRoundTrip_AlphaToV1ToAlpha(t *testing.T) {
	src := &v1alpha1.Timer{
		Spec: v1alpha1.TimerSpec{
			IntervalSeconds: 12,
			TimeoutSeconds:  int64Ptr(0),
			Extra:           map[string]apiextensionsv1.JSON{"k": jsonRaw(t, []byte(`{"x":1}`))},
		},
	}
	hub := &v1.Timer{}
	if err := ConvertV1Alpha1ToV1(src, hub); err != nil {
		t.Fatalf("up: %v", err)
	}
	back := &v1alpha1.Timer{}
	if err := ConvertV1ToV1Alpha1(hub, back); err != nil {
		t.Fatalf("down: %v", err)
	}
	if back.Spec.IntervalSeconds != 12 {
		t.Fatalf("intervalSeconds = %d, want 12", back.Spec.IntervalSeconds)
	}
	if back.Spec.TimeoutSeconds == nil || *back.Spec.TimeoutSeconds != 0 {
		t.Fatalf("timeoutSeconds = %v, want 0", back.Spec.TimeoutSeconds)
	}
	if string(back.Spec.Extra["k"].Raw) != `{"x":1}` {
		t.Fatalf("extra not preserved: %v", back.Spec.Extra)
	}
}

// TestRoundTrip_PriorityStashedInAnnotation is the central "old client must
// not erase new fields" guarantee: v1 priority survives conversion to the
// old version and back via metadata.
func TestRoundTrip_PriorityStashedInAnnotation(t *testing.T) {
	hub := &v1.Timer{Spec: v1.TimerSpec{
		Interval: v1.Duration{Seconds: 1},
		Priority: "High",
	}}
	spoke := &v1alpha1.Timer{}
	if err := ConvertV1ToV1Alpha1(hub, spoke); err != nil {
		t.Fatalf("down: %v", err)
	}
	if got := spoke.Annotations[V1PriorityAnnotation]; got != "High" {
		t.Fatalf("priority annotation = %q, want High", got)
	}

	// Old client updates a field it knows about; metadata round-trips.
	spoke.Spec.IntervalSeconds = 2
	hub2 := &v1.Timer{}
	if err := ConvertV1Alpha1ToV1(spoke, hub2); err != nil {
		t.Fatalf("up: %v", err)
	}
	if hub2.Spec.Priority != "High" {
		t.Fatalf("priority after old-client update = %q, want High", hub2.Spec.Priority)
	}
	if hub2.Spec.Interval.Seconds != 2 {
		t.Fatalf("old-client edit lost: interval = %d", hub2.Spec.Interval.Seconds)
	}
}

func TestConvertUp_CorruptPriorityAnnotationRejected(t *testing.T) {
	src := &v1alpha1.Timer{
		Spec: v1alpha1.TimerSpec{IntervalSeconds: 1},
	}
	src.Annotations = map[string]string{V1PriorityAnnotation: "Bogus"}
	dst := &v1.Timer{}
	err := ConvertV1Alpha1ToV1(src, dst)
	if err == nil || !strings.Contains(err.Error(), "corrupt") {
		t.Fatalf("expected corrupt annotation error, got %v", err)
	}
}

// TestConvertDown_SubSecondRejected is the "no silent truncation" rule:
// every sub-second representation must produce an explicit error.
func TestConvertDown_SubSecondRejected(t *testing.T) {
	cases := []struct {
		name  string
		d     v1.Duration
		where func(*v1.Timer)
	}{
		{"interval-nanos", v1.Duration{Seconds: 0, Nanos: 250_000_000}, func(tm *v1.Timer) { tm.Spec.Interval = v1.Duration{Seconds: 0, Nanos: 250_000_000} }},
		{"interval-nanos-seconds", v1.Duration{Seconds: 5, Nanos: 1}, func(tm *v1.Timer) { tm.Spec.Interval = v1.Duration{Seconds: 5, Nanos: 1} }},
		{"timeout-nanos", v1.Duration{Seconds: 1}, func(tm *v1.Timer) { tm.Spec.Timeout = &v1.Duration{Seconds: 9, Nanos: -1} }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			src := &v1.Timer{Spec: v1.TimerSpec{Interval: v1.Duration{Seconds: 1}}}
			tc.where(src)
			dst := &v1alpha1.Timer{}
			err := ConvertV1ToV1Alpha1(src, dst)
			if err == nil {
				t.Fatalf("expected error for %+v, got nil", tc.d)
			}
			if !strings.Contains(err.Error(), "cannot be expressed in v1alpha1") {
				t.Fatalf("error should mention representability, got: %v", err)
			}
			// Partial output must not be mistaken for success: nothing
			// should have been written into dst.Spec.
			if dst.Spec.IntervalSeconds != 0 {
				t.Fatalf("failed conversion mutated dst: %+v", dst.Spec)
			}
		})
	}
}

func TestConvertDown_MissingTimeoutStaysNil(t *testing.T) {
	src := &v1.Timer{Spec: v1.TimerSpec{Interval: v1.Duration{Seconds: 7}}}
	dst := &v1alpha1.Timer{}
	if err := ConvertV1ToV1Alpha1(src, dst); err != nil {
		t.Fatalf("down: %v", err)
	}
	if dst.Spec.TimeoutSeconds != nil {
		t.Fatalf("timeoutSeconds = %v, want nil", *dst.Spec.TimeoutSeconds)
	}
}

func TestConvertNilInputs(t *testing.T) {
	if err := ConvertV1Alpha1ToV1(nil, &v1.Timer{}); err == nil {
		t.Fatal("nil source should error")
	}
	if err := ConvertV1Alpha1ToV1(&v1alpha1.Timer{}, nil); err == nil {
		t.Fatal("nil destination should error")
	}
	if err := ConvertV1ToV1Alpha1(nil, &v1alpha1.Timer{}); err == nil {
		t.Fatal("nil source should error")
	}
}
