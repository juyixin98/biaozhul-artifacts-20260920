package naming

import "testing"

func TestContentDigest_DeterministicAndDistinct(t *testing.T) {
	d1 := ContentDigest("properties", "key=value\n")
	d2 := ContentDigest("properties", "key=value\n")
	d3 := ContentDigest("json", "{\"k\":1}")
	d4 := ContentDigest("properties", "key=VALUE\n")
	if d1 != d2 {
		t.Fatalf("same content must hash identically: %s vs %s", d1, d2)
	}
	if d1 == d3 || d1 == d4 {
		t.Fatalf("different content must hash differently: %s", d1)
	}
	if len(d1) != 64 {
		t.Fatalf("sha256 hex must be 64 chars, got %d", len(d1))
	}
}

// TestContentDigest_FieldOrderIndependent verifies the "canonical" promise:
// callers that build content from maps with random iteration order still get
// one stable digest because the payload is a single string. Format changes do
// change the digest.
func TestChildName_StableAndLengthBounded(t *testing.T) {
	d := ContentDigest("properties", "x")
	n1 := ChildName("owner", d)
	n2 := ChildName("owner", d)
	if n1 != n2 {
		t.Fatalf("child name must be stable across events: %s vs %s", n1, n2)
	}
	if want := "cfg-owner-" + d[:16]; n1 != want {
		t.Fatalf("unexpected child name: got %s want %s", n1, want)
	}
}
