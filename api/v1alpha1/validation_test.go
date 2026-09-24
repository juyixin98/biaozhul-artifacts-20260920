package v1alpha1

import (
	"strings"
	"testing"
)

func validSpec() ResourceRequestSpec {
	return ResourceRequestSpec{Pool: "default", CPUMilli: 500, MemoryBytes: 1 << 28, TTLSeconds: 60}
}

func TestValidateResourceRequestSpec(t *testing.T) {
	tests := []struct {
		name    string
		mutate  func(*ResourceRequestSpec)
		wantErr string // substring; empty means valid
	}{
		{"valid", nil, ""},
		{"cpu zero", func(s *ResourceRequestSpec) { s.CPUMilli = 0 }, "cpuMilli"},
		{"cpu negative", func(s *ResourceRequestSpec) { s.CPUMilli = -5 }, "cpuMilli"},
		{"cpu above limit", func(s *ResourceRequestSpec) { s.CPUMilli = MaxCPUMilli + 1 }, "exceeds"},
		{"cpu at limit", func(s *ResourceRequestSpec) { s.CPUMilli = MaxCPUMilli }, ""},
		{"memory zero", func(s *ResourceRequestSpec) { s.MemoryBytes = 0 }, "memoryBytes"},
		{"memory above limit", func(s *ResourceRequestSpec) { s.MemoryBytes = MaxMemoryBytes + 1 }, "exceeds"},
		{"memory at limit", func(s *ResourceRequestSpec) { s.MemoryBytes = MaxMemoryBytes }, ""},
		{"ttl too short", func(s *ResourceRequestSpec) { s.TTLSeconds = MinTTLSeconds - 1 }, "ttlSeconds"},
		{"ttl too long", func(s *ResourceRequestSpec) { s.TTLSeconds = MaxTTLSeconds + 1 }, "ttlSeconds"},
		{"ttl at bounds", func(s *ResourceRequestSpec) { s.TTLSeconds = MinTTLSeconds }, ""},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			spec := validSpec()
			if tt.mutate != nil {
				tt.mutate(&spec)
			}
			errs := ValidateResourceRequestSpec(&spec)
			if tt.wantErr == "" {
				if len(errs) != 0 {
					t.Fatalf("want valid, got %v", errs)
				}
				return
			}
			if len(errs) == 0 {
				t.Fatalf("want error containing %q, got none", tt.wantErr)
			}
			if !strings.Contains(errs.ToAggregate().Error(), tt.wantErr) {
				t.Fatalf("error %v does not contain %q", errs, tt.wantErr)
			}
		})
	}
}
