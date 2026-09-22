package util

import (
	"strings"
	"testing"
)

func TestContentHash_StableRegardlessOfJSONKeyOrder(t *testing.T) {
	meta1 := []byte(`{"a":1,"b":{"x":1,"y":2},"c":"z"}`)
	meta2 := []byte(`{"c":"z","b":{"y":2,"x":1},"a":1}`)
	h1 := ContentHash("usb", "emp1", 1700000000000000000, meta1)
	h2 := ContentHash("usb", "emp1", 1700000000000000000, meta2)
	if h1 != h2 {
		t.Fatalf("hashes differ for semantically equal metadata:\n%s\n%s", h1, h2)
	}
	if len(h1) != 64 {
		t.Fatalf("hash length = %d, want 64 hex chars", len(h1))
	}
}

func TestContentHash_DetectsContentDifferences(t *testing.T) {
	base := ContentHash("usb", "emp1", 100, []byte(`{"v":1}`))
	cases := map[string]string{
		"event_type": ContentHash("login", "emp1", 100, []byte(`{"v":1}`)),
		"employee":   ContentHash("usb", "emp2", 100, []byte(`{"v":1}`)),
		"timestamp":  ContentHash("usb", "emp1", 101, []byte(`{"v":1}`)),
		"metadata":   ContentHash("usb", "emp1", 100, []byte(`{"v":2}`)),
	}
	for name, got := range cases {
		if got == base {
			t.Fatalf("hash collision when %s changed", name)
		}
	}
}

func TestContentHash_EmptyAndInvalidMetadata(t *testing.T) {
	h1 := ContentHash("login", "e", 1, nil)
	h2 := ContentHash("login", "e", 1, []byte(`{}`))
	h3 := ContentHash("login", "e", 1, []byte(`not json`))
	if h1 != h2 || h2 != h3 {
		t.Fatalf("empty/object/invalid metadata should normalize equally: %s %s %s", h1, h2, h3)
	}
	if !strings.HasPrefix(h1, "") || len(h1) != 64 {
		t.Fatal("bad hash")
	}
}
