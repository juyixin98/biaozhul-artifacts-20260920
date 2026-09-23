package vec

import (
	"reflect"
	"testing"
)

func TestClockObserveAndHas(t *testing.T) {
	c := Clock{}
	c.Observe(Key{"a", 1})
	if !c.Has(Key{"a", 1}) || c.Has(Key{"a", 2}) {
		t.Fatalf("Has after Observe wrong: %v", c)
	}
	// A delivered-prefix clock: observing 3 means a contiguous 1..3 prefix
	// (the causal layer never lets 3 deliver without 1 and 2).
	c.Observe(Key{"a", 3})
	if !c.Has(Key{"a", 2}) || !c.Has(Key{"a", 3}) || c.Has(Key{"a", 4}) {
		t.Fatalf("prefix semantics wrong: %v", c)
	}
}

func TestMissingDeps(t *testing.T) {
	c := Clock{"a": 2, "b": 1}
	deps := []Key{{"a", 1}, {"a", 2}, {"a", 3}, {"b", 1}, {"c", 1}}
	got := MissingDeps(c, deps)
	want := []Key{{"a", 3}, {"c", 1}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("MissingDeps = %v, want %v", got, want)
	}
}

func TestDepsExplicit(t *testing.T) {
	c := Clock{"a": 2, "b": 1}
	got := c.Deps()
	want := []Key{{"a", 1}, {"a", 2}, {"b", 1}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("Deps = %v, want %v", got, want)
	}
}

func TestMergeAndClone(t *testing.T) {
	c := Clock{"a": 1}
	c.Merge(Clock{"a": 3, "b": 2})
	if c["a"] != 3 || c["b"] != 2 {
		t.Fatalf("merge wrong: %v", c)
	}
	cl := c.Clone()
	cl["a"] = 99
	if c["a"] == 99 {
		t.Fatal("clone aliases the source map")
	}
}
