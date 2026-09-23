package cache

import (
	"encoding/json"
	"path/filepath"
	"testing"
)

func TestPutGet(t *testing.T) {
	c, err := Open(filepath.Join(t.TempDir(), "c"))
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := c.Get("missing"); ok {
		t.Error("missing key should miss")
	}
	if err := c.Put("k1", json.RawMessage(`{"a":1}`)); err != nil {
		t.Fatal(err)
	}
	raw, ok := c.Get("k1")
	if !ok {
		t.Fatal("expected cache hit")
	}
	var v map[string]int
	if err := json.Unmarshal(raw, &v); err != nil || v["a"] != 1 {
		t.Fatalf("bad cached value: %s err=%v", raw, err)
	}
}

func TestKeyCanonical(t *testing.T) {
	req1 := map[string]any{"root": map[string]string{"a": "1"}, "n": 2}
	req2 := map[string]any{"n": 2, "root": map[string]string{"a": "1"}}
	k1, err := Key(req1)
	if err != nil {
		t.Fatal(err)
	}
	k2, err := Key(req2)
	if err != nil {
		t.Fatal(err)
	}
	if k1 != k2 {
		t.Errorf("keys must be order-independent: %s vs %s", k1, k2)
	}
	k3, _ := Key(map[string]any{"n": 3})
	if k1 == k3 {
		t.Error("different requests must hash differently")
	}
}

func TestOpenEmptyDir(t *testing.T) {
	if _, err := Open(""); err == nil {
		t.Error("empty cache dir should error")
	}
}
