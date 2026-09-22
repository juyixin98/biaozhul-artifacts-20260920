package hash

import "testing"

func TestZeroHash(t *testing.T) {
	if len(ZeroHash) != 64 {
		t.Fatalf("zero hash length = %d, want 64", len(ZeroHash))
	}
}

// Two entries with different payloads at the same position hash differently;
// changing either seq or prev_hash also changes the digest.
func TestAuditEntryHashSensitivity(t *testing.T) {
	p1 := []byte(`{"a":1}`)
	p2 := []byte(`{"a":2}`)
	h1 := AuditEntryHash(1, ZeroHash, p1)
	h2 := AuditEntryHash(1, ZeroHash, p2)
	if h1 == h2 {
		t.Fatal("payload change did not change hash")
	}
	if AuditEntryHash(2, ZeroHash, p1) == h1 {
		t.Fatal("seq change did not change hash")
	}
	if AuditEntryHash(1, h1, p1) == AuditEntryHash(1, h2, p1) {
		t.Fatal("prev_hash change did not change hash")
	}
	// Deterministic.
	if AuditEntryHash(1, ZeroHash, p1) != h1 {
		t.Fatal("hash not deterministic")
	}
}

// Chaining: entry 2 references entry 1's hash, so tampering with entry 1
// invalidates every subsequent recomputed link.
func TestChainTamperPropagates(t *testing.T) {
	h1 := AuditEntryHash(1, ZeroHash, []byte(`{"v":1}`))
	h2 := AuditEntryHash(2, h1, []byte(`{"v":2}`))
	h1Forged := AuditEntryHash(1, ZeroHash, []byte(`{"v":999}`))
	h2Recomputed := AuditEntryHash(2, h1Forged, []byte(`{"v":2}`))
	if h2Recomputed == h2 {
		t.Fatal("forged predecessor failed to propagate to the next hash")
	}
}
