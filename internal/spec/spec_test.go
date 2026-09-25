package spec

import (
	"errors"
	"strings"
	"testing"
)

func a(id, tool string, ups map[string]string, outs ...string) Action {
	return Action{ID: id, Tool: tool, Upstream: ups, Outputs: outs}
}

func TestValidateRejectsBadFields(t *testing.T) {
	cases := []struct {
		name string
		p    Project
		want string
	}{
		{"no actions", Project{Name: "p"}, "no actions"},
		{"bad name", Project{Name: "bad/name", Actions: []Action{a("x", "t", nil, "o")}}, "invalid project name"},
		{"empty tool", Project{Name: "p", Actions: []Action{a("x", "", nil, "o")}}, "tool is required"},
		{"no output", Project{Name: "p", Actions: []Action{a("x", "t", nil)}}, "at least one output"},
		{"dup action", Project{Name: "p", Actions: []Action{a("x", "t", nil, "o"), a("x", "t", nil, "o2")}}, "duplicate action id"},
		{"dup output", Project{Name: "p", Actions: []Action{a("x", "t", nil, "o"), a("y", "t", nil, "o")}}, "declared by both"},
		{"unknown upstream", Project{Name: "p", Actions: []Action{a("x", "t", map[string]string{"u": "ghost"}, "o")}}, "unknown action"},
		{"self upstream", Project{Name: "p", Actions: []Action{a("x", "t", map[string]string{"u": "x"}, "o")}}, "depends on itself"},
		{"path escape", Project{Name: "p", Actions: []Action{a("x", "t", nil, "../o")}}, "escapes"},
		{"absolute path", Project{Name: "p", Actions: []Action{a("x", "t", nil, "/tmp/o")}}, "escapes"},
		{"unclean path", Project{Name: "p", Actions: []Action{a("x", "t", nil, "out/../o2")}}, "clean and relative"},
		{"reserved local", Project{Name: "p", Actions: []Action{a("x", "t", nil, "o"), a("y", "t", map[string]string{"src": "x"}, "o2")}}, "reserved"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := tc.p.Validate()
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("Validate() err = %v, want substring %q", err, tc.want)
			}
		})
	}
}

func TestValidateRejectsCycle(t *testing.T) {
	// x -> y -> z -> x
	p := Project{Name: "p", Actions: []Action{
		a("x", "t", map[string]string{"u": "z"}, "ox"),
		a("y", "t", map[string]string{"u": "x"}, "oy"),
		a("z", "t", map[string]string{"u": "y"}, "oz"),
	}}
	err := p.Validate()
	if !errors.Is(err, ErrCycle) {
		t.Fatalf("Validate() err = %v, want ErrCycle", err)
	}
}

func TestTopoOrderDiamond(t *testing.T) {
	// a,b -> c ; a,b share no order dependency with each other.
	p := Project{Name: "p", Actions: []Action{
		a("c", "t", map[string]string{"x": "a", "y": "b"}, "oc"),
		a("a", "t", nil, "oa"),
		a("b", "t", nil, "ob"),
	}}
	order, err := p.TopoOrder()
	if err != nil {
		t.Fatal(err)
	}
	pos := map[string]int{}
	for i, idx := range order {
		pos[p.Actions[idx].ID] = i
	}
	if !(pos["a"] < pos["c"] && pos["b"] < pos["c"]) {
		t.Fatalf("invalid topological order: %v", order)
	}
}
