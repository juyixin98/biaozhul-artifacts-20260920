package chain

import (
	"bytes"
	"errors"
	"testing"
)

func TestBuildAndVerify(t *testing.T) {
	g := Genesis([]byte("seed"))
	if len(g.Hash) != HashLen {
		t.Fatalf("genesis hash len = %d, want %d", len(g.Hash), HashLen)
	}
	if err := VerifyGenesis(g.Hash, g); err != nil {
		t.Fatalf("genesis self-verify: %v", err)
	}
	blocks := []Block{g}
	parent := g
	for h := 1; h <= 10; h++ {
		b := Child(parent, int64(100+h), []byte{byte(h)})
		blocks = append(blocks, b)
		parent = b
	}
	if err := VerifyChain(g.Hash, blocks); err != nil {
		t.Fatalf("VerifyChain: %v", err)
	}
}

func TestTamperDetected(t *testing.T) {
	g := Genesis([]byte("seed"))
	b1 := Child(g, 1, []byte("a"))
	b2 := Child(b1, 2, []byte("b"))

	// Mutate a body byte without updating the hash: must be caught.
	tampered := b2.Clone()
	tampered.Body[0] ^= 0xFF
	err := VerifyAppend(b1.Hash, 2, []Block{tampered})
	if !errors.Is(err, ErrHashMismatch) {
		t.Fatalf("want ErrHashMismatch, got %v", err)
	}

	// A self-consistent fork anchored at a bogus parent passes internal
	// verification but fails the trusted-anchor append check.
	fork := b2.Clone()
	fork.ParentHash = bytes.Repeat([]byte{0xEE}, HashLen)
	fork.Rehash()
	if err := VerifyInternal([]Block{fork}); err != nil {
		t.Fatalf("fork should be internally consistent, got %v", err)
	}
	if err := VerifyAppend(b1.Hash, 2, []Block{fork}); !errors.Is(err, ErrParentMismatch) {
		t.Fatalf("want ErrParentMismatch at boundary, got %v", err)
	}
}

func TestGapAndReorderedReject(t *testing.T) {
	g := Genesis([]byte("seed"))
	b1 := Child(g, 1, nil)
	b2 := Child(b1, 2, nil)
	b4 := Child(Child(b2, 3, nil), 4, nil)

	// Skipping height 3 must be rejected as a height gap: ask for height 4
	// where the next expected height is 3.
	if err := VerifyAppend(b2.Hash, 3, []Block{b4}); !errors.Is(err, ErrHeightGap) {
		t.Fatalf("want ErrHeightGap, got %v", err)
	}
	// A valid height-4 block appended directly after height 2 fails the link.
	if err := VerifyAppend(b2.Hash, 4, []Block{b4}); !errors.Is(err, ErrParentMismatch) {
		t.Fatalf("want ErrParentMismatch (missing block 3), got %v", err)
	}
}

func TestCanonicalIsUnambiguous(t *testing.T) {
	h1 := HashFor(1, bytes.Repeat([]byte{1}, HashLen), 2, []byte("abc"))
	h2 := HashFor(1, bytes.Repeat([]byte{1}, HashLen), 2, []byte("abc"))
	if !bytes.Equal(h1, h2) {
		t.Fatal("hashing not deterministic")
	}
	h3 := HashFor(1, bytes.Repeat([]byte{2}, HashLen), 2, []byte("abc"))
	if bytes.Equal(h1, h3) {
		t.Fatal("different parent produced same hash")
	}
}
