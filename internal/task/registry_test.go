package task_test

import (
	"context"
	"strings"
	"testing"

	"dagexec/internal/task"
)

func TestBuiltinRegistryWhitelist(t *testing.T) {
	reg := task.Builtins()
	for _, name := range []string{"const", "add", "mul", "div", "sum_deps", "concat", "length", "fail"} {
		if _, ok := reg.Get(name); !ok {
			t.Errorf("builtin %q missing", name)
		}
	}
	if _, ok := reg.Get("exec"); ok {
		t.Error("non-whitelisted task available")
	}
}

func TestArithmeticBuiltins(t *testing.T) {
	ctx := context.Background()
	reg := task.Builtins()

	run := func(name string, params, deps map[string]any) (any, error) {
		fn, _ := reg.Get(name)
		return fn(ctx, params, deps)
	}

	out, err := run("add", map[string]any{"x": 2, "y": 3}, nil)
	if err != nil || out.(int64) != 5 {
		t.Fatalf("add=%v,%v", out, err)
	}
	out, err = run("mul", map[string]any{"x": 6, "y": 7}, nil)
	if err != nil || out.(int64) != 42 {
		t.Fatalf("mul=%v,%v", out, err)
	}
	if _, err := run("div", map[string]any{"x": 1, "y": 0}, nil); err == nil {
		t.Error("div by zero must error")
	}
	if _, err := run("add", map[string]any{"x": "a", "y": 1}, nil); err == nil {
		t.Error("type error must be reported")
	}

	out, err = run("sum_deps", nil, map[string]any{"a": float64(1), "b": float64(2), "c": float64(3)})
	if err != nil || out.(int64) != 6 {
		t.Fatalf("sum_deps=%v,%v", out, err)
	}

	out, err = run("concat", map[string]any{
		"sep":    "-",
		"values": []any{"a", "b", "c"},
	}, nil)
	if err != nil || out != "a-b-c" {
		t.Fatalf("concat=%v,%v", out, err)
	}

	out, err = run("length", map[string]any{"value": "héllo"}, nil)
	if err != nil || out.(int64) != 5 {
		t.Fatalf("length runes=%v,%v", out, err)
	}
}

func TestResolveParamsRefs(t *testing.T) {
	deps := map[string]any{"up": int64(99)}

	got, err := task.ResolveParams(
		map[string]any{"x": map[string]any{"$ref": "up"}, "y": 2}, deps)
	if err != nil {
		t.Fatal(err)
	}
	if got["x"].(int64) != 99 {
		t.Errorf("ref resolved to %v", got["x"])
	}

	// Nested refs in arrays/maps.
	got, err = task.ResolveParams(
		map[string]any{"arr": []any{map[string]any{"$ref": "up"}}}, deps)
	if err != nil {
		t.Fatal(err)
	}
	if got["arr"].([]any)[0].(int64) != 99 {
		t.Errorf("nested ref=%v", got["arr"])
	}

	// Reference to a node not in dep results is an error.
	_, err = task.ResolveParams(map[string]any{"x": map[string]any{"$ref": "ghost"}}, deps)
	if err == nil || !strings.Contains(err.Error(), "not a succeeded dependency") {
		t.Fatalf("expected unresolved ref error, got %v", err)
	}
}

func TestFailBuiltinAlwaysErrors(t *testing.T) {
	fn, _ := task.Builtins().Get("fail")
	_, err := fn(context.Background(), map[string]any{"message": "nope"}, nil)
	if err == nil || err.Error() != "nope" {
		t.Fatalf("err=%v", err)
	}
}
