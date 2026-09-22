package canonical

import (
	"testing"
)

func TestJSONSortsKeys(t *testing.T) {
	a, _ := JSON(map[string]any{"z": 1, "a": map[string]any{"y": true, "b": 2}})
	b, _ := JSON(map[string]any{"a": map[string]any{"b": 2, "y": true}, "z": 1})
	if string(a) != string(b) {
		t.Fatalf("canonical forms differ:\n%s\n%s", a, b)
	}
	want := `{"a":{"b":2,"y":true},"z":1}`
	if string(a) != want {
		t.Fatalf("canonical = %s, want %s", a, want)
	}
}

func TestJSONPreservesArrayOrder(t *testing.T) {
	a, _ := JSON([]any{3, 1, 2})
	if string(a) != "[3,1,2]" {
		t.Fatalf("array order changed: %s", a)
	}
}
