package graph

import (
	"reflect"
	"strings"
	"testing"
)

func shellTool(name, ver string) *Tool {
	return &Tool{Name: name, ToolVersion: ver, Command: []string{"true"}, Shell: true}
}

func TestTopoOrderRespectsDeps(t *testing.T) {
	spec := &Spec{
		Version: "1",
		Tools:   map[string]*Tool{"t": shellTool("t", "1")},
		Nodes: []*Node{
			{Name: "a", Tool: "t"},
			{Name: "b", Tool: "t", Deps: []string{"a"}},
			{Name: "c", Tool: "t", Deps: []string{"b", "a"}},
		},
	}
	if err := spec.Validate(); err != nil {
		t.Fatal(err)
	}
	reach, _ := spec.Reachable(nil)
	order, err := spec.TopoOrder(reach)
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"a", "b", "c"}
	if !reflect.DeepEqual(order, want) {
		t.Fatalf("order = %v, want %v", order, want)
	}
}

func TestTopoOrderDeterministic(t *testing.T) {
	// 多根、无相互依赖 => 按名字排序。
	spec := &Spec{
		Version: "1",
		Tools:   map[string]*Tool{"t": shellTool("t", "1")},
		Nodes: []*Node{
			{Name: "zeta", Tool: "t"},
			{Name: "alpha", Tool: "t"},
			{Name: "mid", Tool: "t"},
		},
	}
	if err := spec.Validate(); err != nil {
		t.Fatal(err)
	}
	reach, _ := spec.Reachable(nil)
	for i := 0; i < 5; i++ {
		order, err := spec.TopoOrder(reach)
		if err != nil {
			t.Fatal(err)
		}
		if want := []string{"alpha", "mid", "zeta"}; !reflect.DeepEqual(order, want) {
			t.Fatalf("iter %d: order = %v, want %v", i, order, want)
		}
	}
}

func TestCycleDetected(t *testing.T) {
	spec := &Spec{
		Version: "1",
		Tools:   map[string]*Tool{"t": shellTool("t", "1")},
		Nodes: []*Node{
			{Name: "a", Tool: "t", Deps: []string{"c"}},
			{Name: "b", Tool: "t", Deps: []string{"a"}},
			{Name: "c", Tool: "t", Deps: []string{"b"}},
		},
	}
	if err := spec.Validate(); err != nil {
		t.Fatal(err)
	}
	reach, _ := spec.Reachable(nil)
	_, err := spec.TopoOrder(reach)
	ce, ok := err.(*CycleError)
	if !ok {
		t.Fatalf("want *CycleError, got %v", err)
	}
	if len(ce.Cycle) < 3 || ce.Cycle[0] != ce.Cycle[len(ce.Cycle)-1] {
		t.Fatalf("bad cycle: %v", ce.Cycle)
	}
	if !strings.Contains(ce.Error(), "cycle") {
		t.Fatalf("error message should mention cycle: %q", ce.Error())
	}
}

func TestSelfCycleValidation(t *testing.T) {
	spec := &Spec{
		Version: "1",
		Tools:   map[string]*Tool{"t": shellTool("t", "1")},
		Nodes:   []*Node{{Name: "a", Tool: "t", Deps: []string{"a"}}},
	}
	if err := spec.Validate(); err == nil {
		t.Fatal("self dependency should fail validation")
	}
}

func TestReachableTargets(t *testing.T) {
	spec := &Spec{
		Version: "1",
		Tools:   map[string]*Tool{"t": shellTool("t", "1")},
		Nodes: []*Node{
			{Name: "a", Tool: "t"},
			{Name: "b", Tool: "t", Deps: []string{"a"}},
			{Name: "unrelated", Tool: "t"},
		},
	}
	if err := spec.Validate(); err != nil {
		t.Fatal(err)
	}
	reach, err := spec.Reachable([]string{"b"})
	if err != nil {
		t.Fatal(err)
	}
	if !reach["a"] || !reach["b"] || reach["unrelated"] {
		t.Fatalf("reachable set wrong: %v", reach)
	}
}

func TestValidateUnknownToolAndDeps(t *testing.T) {
	spec := &Spec{
		Version: "1",
		Tools:   map[string]*Tool{"t": shellTool("t", "1")},
		Nodes:   []*Node{{Name: "a", Tool: "missing"}},
	}
	if err := spec.Validate(); err == nil {
		t.Fatal("unknown tool should fail")
	}

	spec2 := &Spec{
		Version: "1",
		Tools:   map[string]*Tool{"t": shellTool("t", "1")},
		Nodes:   []*Node{{Name: "a", Tool: "t", Deps: []string{"ghost"}}},
	}
	if err := spec2.Validate(); err == nil {
		t.Fatal("unknown dep should fail")
	}
}

func TestValidatePathTraversal(t *testing.T) {
	spec := &Spec{
		Version: "1",
		Tools:   map[string]*Tool{"t": shellTool("t", "1")},
		Nodes:   []*Node{{Name: "a", Tool: "t", Inputs: []string{"../secret"}}},
	}
	if err := spec.Validate(); err == nil {
		t.Fatal("path escaping workdir should fail")
	}
}

func TestValidateTemplateParam(t *testing.T) {
	spec := &Spec{
		Version: "1",
		Tools:   map[string]*Tool{"t": shellTool("t", "1")},
		Nodes: []*Node{{
			Name: "a", Tool: "t",
			Args:   []string{"{{.undefined_key}}"},
			Params: map[string]string{"other": "x"},
		}},
	}
	if err := spec.Validate(); err == nil || !strings.Contains(err.Error(), "undefined param") {
		t.Fatalf("want undefined param error, got %v", err)
	}
	// 合法模板通过。
	spec.Nodes[0].Args = []string{"{{.other}}"}
	if err := spec.Validate(); err != nil {
		t.Fatal(err)
	}
}
