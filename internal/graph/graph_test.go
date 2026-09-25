package graph

import (
	"testing"
)

func TestFindCycleNone(t *testing.T) {
	g := &Graph{Nodes: []*Node{
		{ID: "a"},
		{ID: "b", DependsOn: []string{"a"}},
		{ID: "c", DependsOn: []string{"a", "b"}},
	}}
	if cyc := g.FindCycle(); cyc != nil {
		t.Fatalf("unexpected cycle: %v", cyc)
	}
	if err := g.Validate(); err != nil {
		t.Fatalf("validate: %v", err)
	}
}

func TestFindCycleDirectAndLong(t *testing.T) {
	g := &Graph{Nodes: []*Node{
		{ID: "a", DependsOn: []string{"c"}},
		{ID: "b", DependsOn: []string{"a"}},
		{ID: "c", DependsOn: []string{"b"}},
	}}
	cyc := g.FindCycle()
	if cyc == nil {
		t.Fatal("expected cycle, got nil")
	}
	if cyc[0] != cyc[len(cyc)-1] {
		t.Fatalf("cycle must close: %v", cyc)
	}
	err := g.Validate()
	if err == nil || err.Kind != "cycle" {
		t.Fatalf("want cycle error, got %v", err)
	}
}

func TestSelfCycle(t *testing.T) {
	g := &Graph{Nodes: []*Node{{ID: "a", DependsOn: []string{"a"}}}}
	if err := g.Validate(); err == nil || err.Kind != "cycle" {
		t.Fatalf("want cycle, got %v", err)
	}
}

func TestUnknownDependency(t *testing.T) {
	g := &Graph{Nodes: []*Node{{ID: "a", DependsOn: []string{"ghost"}}}}
	err := g.Validate()
	if err == nil || err.Kind != "unknown_dependency" {
		t.Fatalf("want unknown_dependency, got %v", err)
	}
}

func TestDuplicateNode(t *testing.T) {
	g := &Graph{Nodes: []*Node{{ID: "a"}, {ID: "a"}}}
	if err := g.Validate(); err == nil || err.Kind != "duplicate_node" {
		t.Fatalf("want duplicate_node, got %v", err)
	}
}

func TestPathEscape(t *testing.T) {
	cases := []string{"../x", "a/../../b", "/abs/path"}
	for _, p := range cases {
		g := &Graph{Nodes: []*Node{{ID: "a", Inputs: []string{p}}}}
		err := g.Validate()
		if err == nil {
			t.Fatalf("path %q should be rejected", p)
		}
	}
}

func TestTopoOrder(t *testing.T) {
	g := &Graph{Nodes: []*Node{
		{ID: "c", DependsOn: []string{"b"}},
		{ID: "a"},
		{ID: "b", DependsOn: []string{"a"}},
		{ID: "d", DependsOn: []string{"a"}},
	}}
	order, err := g.Topo()
	if err != nil {
		t.Fatal(err)
	}
	pos := map[string]int{}
	for i, id := range order {
		pos[id] = i
	}
	if !(pos["a"] < pos["b"] && pos["b"] < pos["c"] && pos["a"] < pos["d"]) {
		t.Fatalf("bad order: %v", order)
	}
	// Alphabetical tie-break: d before its sibling-level nodes that are
	// only constrained later; check deterministic repeat.
	order2, _ := g.Topo()
	for i := range order {
		if order[i] != order2[i] {
			t.Fatalf("topo not deterministic: %v vs %v", order, order2)
		}
	}
}

func TestClosure(t *testing.T) {
	g := &Graph{Nodes: []*Node{
		{ID: "a"},
		{ID: "b", DependsOn: []string{"a"}},
		{ID: "c", DependsOn: []string{"b"}},
		{ID: "lonely"},
	}}
	set, err := g.Closure([]string{"c"})
	if err != nil {
		t.Fatal(err)
	}
	if !set["a"] || !set["b"] || !set["c"] || set["lonely"] {
		t.Fatalf("bad closure: %v", set)
	}
	if _, err := g.Closure([]string{"nope"}); err == nil || err.Kind != "unknown_target" {
		t.Fatalf("want unknown_target, got %v", err)
	}
}
