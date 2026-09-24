package oci

import (
	"strings"
	"testing"
)

func TestDetectCycleNone(t *testing.T) {
	adj := map[string][]string{
		"root": {"a", "b"},
		"a":    {"c"},
		"b":    {"c"}, // diamond: shared node, not a cycle
		"c":    {},
	}
	if err := DetectCycle(adj); err != nil {
		t.Fatalf("unexpected cycle: %v", err)
	}
}

func TestDetectCycleSelfLoop(t *testing.T) {
	adj := map[string][]string{"a": {"a"}}
	err := DetectCycle(adj)
	if err == nil || !strings.Contains(err.Error(), "circular reference") {
		t.Fatalf("want circular reference error, got %v", err)
	}
}

func TestDetectCycleIndirect(t *testing.T) {
	adj := map[string][]string{
		"a": {"b"},
		"b": {"c"},
		"c": {"a"},
	}
	if err := DetectCycle(adj); err == nil {
		t.Fatal("want cycle error, got nil")
	}
}
