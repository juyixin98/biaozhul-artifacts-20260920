package dag

import (
	"strings"
	"testing"
)

func n(id string, task string, deps ...string) NodeSpec {
	return NodeSpec{ID: id, Task: task, Deps: deps}
}

func TestValidateOK(t *testing.T) {
	spec := Spec{Nodes: []NodeSpec{
		n("a", "noop"),
		n("b", "add", "a"),
		n("c", "mul", "a"),
		n("d", "concat", "b", "c"),
	}}
	if err := spec.Validate(DefaultRegistry()); err != nil {
		t.Fatalf("expected valid spec, got %v", err)
	}
}

func TestValidateMissingDependency(t *testing.T) {
	spec := Spec{Nodes: []NodeSpec{n("a", "noop", "ghost")}}
	err := spec.Validate(DefaultRegistry())
	var ve *ValidationError
	if !asValidation(err, &ve) {
		t.Fatalf("expected ValidationError, got %v", err)
	}
	if !strings.Contains(err.Error(), "missing node \"ghost\"") {
		t.Fatalf("expected missing-dependency message, got %v", err)
	}
}

func TestValidateCycle(t *testing.T) {
	spec := Spec{Nodes: []NodeSpec{
		n("a", "noop", "c"),
		n("b", "noop", "a"),
		n("c", "noop", "b"),
	}}
	err := spec.Validate(DefaultRegistry())
	var ve *ValidationError
	if !asValidation(err, &ve) {
		t.Fatalf("expected ValidationError, got %v", err)
	}
	if !strings.Contains(err.Error(), "cycle") {
		t.Fatalf("expected cycle message, got %v", err)
	}
}

func TestValidateSelfLoop(t *testing.T) {
	spec := Spec{Nodes: []NodeSpec{n("a", "noop", "a")}}
	if err := spec.Validate(DefaultRegistry()); err == nil ||
		!strings.Contains(err.Error(), "cycle") {
		t.Fatalf("expected self-loop cycle error, got %v", err)
	}
}

func TestValidateUnknownTask(t *testing.T) {
	spec := Spec{Nodes: []NodeSpec{n("a", "rm-rf")}}
	err := spec.Validate(DefaultRegistry())
	if err == nil || !strings.Contains(err.Error(), "not in the whitelist") {
		t.Fatalf("expected whitelist error, got %v", err)
	}
}

func TestValidateEmptyAndDuplicateIDs(t *testing.T) {
	if err := (&Spec{Nodes: nil}).Validate(DefaultRegistry()); err == nil {
		t.Fatal("expected error on empty nodes")
	}
	spec := Spec{Nodes: []NodeSpec{
		n("a", "noop"),
		n("a", "noop"),
	}}
	if err := spec.Validate(DefaultRegistry()); err == nil ||
		!strings.Contains(err.Error(), "duplicate node id") {
		t.Fatalf("expected duplicate id error, got %v", err)
	}
}

func TestValidateDuplicateDep(t *testing.T) {
	spec := Spec{Nodes: []NodeSpec{
		n("a", "noop"),
		{ID: "b", Task: "add", Deps: []string{"a", "a"}},
	}}
	if err := spec.Validate(DefaultRegistry()); err == nil ||
		!strings.Contains(err.Error(), "duplicate dependency") {
		t.Fatalf("expected duplicate dependency error, got %v", err)
	}
}

func asValidation(err error, target **ValidationError) bool {
	if err == nil {
		return false
	}
	ve, ok := err.(*ValidationError)
	if !ok {
		return false
	}
	*target = ve
	return true
}
