package validation

import (
	"strings"
	"testing"

	v1 "github.com/example/crd-migration-demo/api/v1"
	v1alpha1 "github.com/example/crd-migration-demo/api/v1alpha1"
)

func TestValidateV1_OK(t *testing.T) {
	cases := []v1.Timer{
		{Spec: v1.TimerSpec{Interval: v1.Duration{Seconds: 1}, Priority: "Normal"}},
		{Spec: v1.TimerSpec{Interval: v1.Duration{Seconds: 0, Nanos: 1}}}, // sub-second positive
		{Spec: v1.TimerSpec{Interval: v1.Duration{Seconds: 1}, Timeout: &v1.Duration{Seconds: 0}, Priority: "High"}},
	}
	for i, tm := range cases {
		if err := ValidateV1(&tm); err != nil {
			t.Fatalf("case %d: expected valid, got %v", i, err)
		}
	}
}

func TestValidateV1_InvalidDurations(t *testing.T) {
	cases := []struct {
		name string
		spec v1.TimerSpec
		want string
	}{
		{"zero interval", v1.TimerSpec{Interval: v1.Duration{Seconds: 0}}, "strictly positive"},
		{"negative seconds", v1.TimerSpec{Interval: v1.Duration{Seconds: -1}}, "strictly positive"},
		{"nanos too big", v1.TimerSpec{Interval: v1.Duration{Seconds: 1, Nanos: 1_000_000_000}}, "nanos"},
		{"nanos too small", v1.TimerSpec{Interval: v1.Duration{Seconds: 1, Nanos: -1_000_000_000}}, "nanos"},
		{"sign mismatch +-", v1.TimerSpec{Interval: v1.Duration{Seconds: 1, Nanos: -1}}, "same sign"},
		{"sign mismatch -+", v1.TimerSpec{Interval: v1.Duration{Seconds: -1, Nanos: 1}}, "same sign"},
		{"bad priority", v1.TimerSpec{Interval: v1.Duration{Seconds: 1}, Priority: "Urgent"}, "priority"},
		{"bad timeout sign", v1.TimerSpec{
			Interval: v1.Duration{Seconds: 1},
			Timeout:  &v1.Duration{Seconds: 2, Nanos: -500},
		}, "same sign"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := ValidateV1(&v1.Timer{Spec: tc.spec})
			if err == nil {
				t.Fatalf("expected error containing %q, got nil", tc.want)
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("error = %q, want substring %q", err.Error(), tc.want)
			}
		})
	}
}

func TestValidateV1Alpha1(t *testing.T) {
	one := int64(1)
	zero := int64(0)
	neg := int64(-2)
	if err := ValidateV1Alpha1(&v1alpha1.Timer{Spec: v1alpha1.TimerSpec{IntervalSeconds: 1}}); err != nil {
		t.Fatalf("valid object rejected: %v", err)
	}
	if err := ValidateV1Alpha1(&v1alpha1.Timer{Spec: v1alpha1.TimerSpec{
		IntervalSeconds: 1, TimeoutSeconds: &zero,
	}}); err != nil {
		t.Fatalf("zero timeout should be allowed: %v", err)
	}
	if err := ValidateV1Alpha1(&v1alpha1.Timer{Spec: v1alpha1.TimerSpec{IntervalSeconds: 0}}); err == nil {
		t.Fatal("intervalSeconds=0 must be rejected")
	}
	if err := ValidateV1Alpha1(&v1alpha1.Timer{Spec: v1alpha1.TimerSpec{
		IntervalSeconds: one, TimeoutSeconds: &neg,
	}}); err == nil {
		t.Fatal("negative timeout must be rejected")
	}
}
