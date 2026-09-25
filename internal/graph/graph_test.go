package graph

import (
	"reflect"
	"testing"
)

func TestSetDedupsAndSorts(t *testing.T) {
	g := New()
	g.Set("a.c", []string{"c.h", "b.h", "b.h"})
	got := g.Deps["a.c"]
	want := []string{"b.h", "c.h"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("got %v, want %v", got, want)
	}
}

func TestSetKeepsSelfEdge(t *testing.T) {
	g := New()
	g.Set("guard.h", []string{"guard.h", "x.h"})
	got := g.Deps["guard.h"]
	want := []string{"guard.h", "x.h"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("got %v, want %v (self-include is legal)", got, want)
	}
}

func TestAffectedReverseClosure(t *testing.T) {
	g := New()
	// a.c -> base.h -> deep.h ; b.c -> base.h ; unrel.c alone
	g.Set("a.c", []string{"base.h"})
	g.Set("b.c", []string{"base.h"})
	g.Set("base.h", []string{"deep.h"})
	g.Set("deep.h", nil)
	g.Set("unrel.c", []string{"other.h"})

	got := g.Affected([]string{"deep.h"})
	want := []string{"a.c", "b.c", "base.h", "deep.h"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("got %v, want %v", got, want)
	}

	got = g.Affected([]string{"other.h"})
	want = []string{"other.h", "unrel.c"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("got %v, want %v", got, want)
	}
}

func TestCycles(t *testing.T) {
	g := New()
	g.Set("x.h", []string{"y.h"})
	g.Set("y.h", []string{"x.h", "z.h"})
	g.Set("z.h", nil)
	g.Set("solo.c", []string{"solo.c"}) // self-loop

	got := g.Cycles()
	want := [][]string{
		{"solo.c"},
		{"x.h", "y.h"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("got %v, want %v", got, want)
	}
}

func TestRemoveNodeAndInboundEdges(t *testing.T) {
	g := New()
	g.Set("a.c", []string{"gone.h", "kept.h"})
	g.Set("b.c", []string{"gone.h"})
	g.Set("gone.h", nil)
	g.Set("kept.h", nil)

	g.Remove("gone.h")
	if _, ok := g.Deps["gone.h"]; ok {
		t.Error("node gone.h should be removed")
	}
	if !reflect.DeepEqual(g.Deps["a.c"], []string{"kept.h"}) {
		t.Errorf("a.c deps = %v, want [kept.h]", g.Deps["a.c"])
	}
	if len(g.Deps["b.c"]) != 0 {
		t.Errorf("b.c deps = %v, want empty", g.Deps["b.c"])
	}
}
