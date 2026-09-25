package canonical

import (
	"strings"
	"testing"
)

func TestEncodeIsDeterministic(t *testing.T) {
	a := map[string]any{"b": 1, "a": []any{3, 2, 1}, "c": map[string]any{"z": true, "y": "x"}}
	one, err := Encode(a)
	if err != nil {
		t.Fatal(err)
	}
	two, err := Encode(a)
	if err != nil {
		t.Fatal(err)
	}
	if string(one) != string(two) {
		t.Fatalf("encoding not stable:\n%s\n%s", one, two)
	}
	if strings.ContainsAny(string(one), " \n\t") {
		t.Fatalf("canonical encoding must have no insignificant whitespace: %s", one)
	}
	want := `{"a":[3,2,1],"b":1,"c":{"y":"x","z":true}}`
	if string(one) != want {
		t.Fatalf("got %s, want %s", one, want)
	}
}

func TestEncodeKeyOrderingIsLexicographic(t *testing.T) {
	m := map[string]any{"zz": 0, "aa": 0, "mm": 0}
	b, err := Encode(m)
	if err != nil {
		t.Fatal(err)
	}
	if string(b) != `{"aa":0,"mm":0,"zz":0}` {
		t.Fatalf("got %s", b)
	}
}

func TestEncodeIdenticalContentSameDigest(t *testing.T) {
	m1 := map[string]any{"x": []string{"a", "b"}, "y": "z"}
	m2 := map[string]any{"y": "z", "x": []string{"a", "b"}}
	b1, _ := Encode(m1)
	b2, _ := Encode(m2)
	if string(b1) != string(b2) {
		t.Fatalf("equal documents must encode identically:\n%s\n%s", b1, b2)
	}
}
