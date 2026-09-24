package canon_test

import (
	"encoding/json"
	"testing"

	"atomicpromo/internal/crypto/canon"
)

func TestCanonicalDeterministic(t *testing.T) {
	m1 := map[string]any{"b": 1, "a": []any{3, 1, 2}, "c": map[string]any{"z": true, "y": "x"}}
	b1, err := canon.Encode(m1)
	if err != nil {
		t.Fatal(err)
	}
	// 不同插入顺序
	m2 := map[string]any{"c": map[string]any{"y": "x", "z": true}, "a": []any{3, 1, 2}, "b": 1}
	b2, _ := canon.Encode(m2)
	if string(b1) != string(b2) {
		t.Fatalf("non-deterministic: %s vs %s", b1, b2)
	}
	want := `{"a":[3,1,2],"b":1,"c":{"y":"x","z":true}}`
	if string(b1) != want {
		t.Fatalf("got %s want %s", b1, want)
	}
}

func TestCanonicalRawMessage(t *testing.T) {
	raw := json.RawMessage(`{"z":1,"a":{"y":2,"b":3}}`)
	got, err := canon.Encode(raw)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"a":{"b":3,"y":2},"z":1}`
	if string(got) != want {
		t.Fatalf("got %s want %s", got, want)
	}
}

func TestCanonicalStableAcrossForms(t *testing.T) {
	// 同一语义内容：map 形式与 json.RawMessage 形式必须得到同一字节串
	m := map[string]any{"k": "v", "n": json.Number("100")}
	b1, _ := canon.Encode(m)
	b2, _ := canon.Encode(json.RawMessage(`{"n":100,"k":"v"}`))
	if string(b1) != string(b2) {
		t.Fatalf("forms differ: %s vs %s", b1, b2)
	}
}
