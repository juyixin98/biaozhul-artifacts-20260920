package engine

import (
	"strings"
	"testing"

	"dagexec/internal/model"
	"dagexec/internal/task"
)

func TestFindCycle(t *testing.T) {
	noCycle := []model.Node{
		{ID: "a"}, {ID: "b", Deps: []string{"a"}}, {ID: "c", Deps: []string{"a", "b"}},
	}
	if cyc := findCycle(noCycle); cyc != nil {
		t.Fatalf("unexpected cycle: %v", cyc)
	}

	direct := []model.Node{{ID: "a", Deps: []string{"b"}}, {ID: "b", Deps: []string{"a"}}}
	if cyc := findCycle(direct); len(cyc) < 2 || cyc[0] != cyc[len(cyc)-1] {
		t.Fatalf("malformed cycle report: %v", cyc)
	}

	three := []model.Node{
		{ID: "a", Deps: []string{"c"}},
		{ID: "b", Deps: []string{"a"}},
		{ID: "c", Deps: []string{"b"}},
	}
	cyc := findCycle(three)
	if cyc == nil {
		t.Fatal("3-node cycle not found")
	}
	// Every node in the reported chain is one of the three.
	for _, n := range cyc {
		if n != "a" && n != "b" && n != "c" {
			t.Fatalf("cycle report contains unknown node %q: %v", n, cyc)
		}
	}
}

func TestValidateSpecErrors(t *testing.T) {
	reg := task.Builtins()
	good := model.Node{ID: "a", Type: "const", Params: map[string]any{"value": 1}}

	cases := []struct {
		name string
		dag  model.DAG
		want string
	}{
		{"empty", model.DAG{}, "no nodes"},
		{"bad id", model.DAG{Nodes: []model.Node{{ID: "bad id!", Type: "const"}}}, "id must match"},
		{"unknown type", model.DAG{Nodes: []model.Node{{ID: "a", Type: "shell"}}}, "unknown task type"},
		{"missing dep", model.DAG{Nodes: []model.Node{
			good, {ID: "b", Type: "const", Deps: []string{"x"}, Params: map[string]any{"value": 1}},
		}}, "missing dependency"},
		{"cycle", model.DAG{Nodes: []model.Node{
			{ID: "a", Type: "const", Deps: []string{"b"}, Params: map[string]any{"value": 1}},
			{ID: "b", Type: "const", Deps: []string{"a"}, Params: map[string]any{"value": 1}},
		}}, "cycle detected"},
		{"duplicate dep", model.DAG{Nodes: []model.Node{
			good, {ID: "b", Type: "const", Deps: []string{"a", "a"}, Params: map[string]any{"value": 1}},
		}}, "duplicate dependency"},
		{"negative retries", model.DAG{Retries: -1, Nodes: []model.Node{good}}, "retries"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := validateSpec(tc.dag, reg)
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("err=%v want substring %q", err, tc.want)
			}
		})
	}
}
