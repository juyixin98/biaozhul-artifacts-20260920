package merkle

import (
	"crypto/sha256"
	"fmt"
	"testing"

	"inbox/internal/crypto"
)

func leaf(s string) crypto.Hash { return sha256.Sum256([]byte(s)) }

func TestEmptyRootStable(t *testing.T) {
	if Root(nil) != EmptyRoot {
		t.Fatal("root of no leaves must be EmptyRoot")
	}
	if err := Verify(EmptyRoot, leaf("x"), Proof{}); err == nil {
		t.Fatal("proof against empty root must fail")
	}
}

func TestSingleLeaf(t *testing.T) {
	l := leaf("only")
	root := Root([]crypto.Hash{l})
	if root != l {
		t.Fatal("single-leaf root must equal the leaf")
	}
	p, err := BuildProof([]crypto.Hash{l}, 0)
	if err != nil {
		t.Fatal(err)
	}
	if err := Verify(root, l, p); err != nil {
		t.Fatalf("verify: %v", err)
	}
}

func TestInclusionProofsAllSizes(t *testing.T) {
	for _, n := range []int{1, 2, 3, 4, 5, 7, 8, 9, 16, 17} {
		leaves := make([]crypto.Hash, n)
		for i := range leaves {
			leaves[i] = leaf(fmt.Sprintf("leaf-%d", i))
		}
		root := Root(leaves)
		for i := range leaves {
			p, err := BuildProof(leaves, i)
			if err != nil {
				t.Fatalf("n=%d i=%d build: %v", n, i, err)
			}
			if err := Verify(root, leaves[i], p); err != nil {
				t.Fatalf("n=%d i=%d verify: %v", n, i, err)
			}
		}
	}
}

func TestProofRejectsForeignLeaf(t *testing.T) {
	leaves := []crypto.Hash{leaf("a"), leaf("b"), leaf("c"), leaf("d")}
	root := Root(leaves)
	p, err := BuildProof(leaves, 1)
	if err != nil {
		t.Fatal(err)
	}
	if err := Verify(root, leaf("forged"), p); err == nil {
		t.Fatal("forged leaf must not verify")
	}
	// Proof built for index 1 must not verify leaf 2.
	if err := Verify(root, leaves[2], p); err == nil {
		t.Fatal("proof for index 1 must not verify leaf at index 2")
	}
}

func TestOddNodeDuplication(t *testing.T) {
	// Three leaves exercises the odd-node duplication path at two levels.
	leaves := []crypto.Hash{leaf("a"), leaf("b"), leaf("c")}
	root := Root(leaves)
	for i := range leaves {
		p, _ := BuildProof(leaves, i)
		if err := Verify(root, leaves[i], p); err != nil {
			t.Fatalf("i=%d: %v", i, err)
		}
	}
	// Reordering leaves changes the root.
	reordered := []crypto.Hash{leaves[0], leaves[2], leaves[1]}
	if Root(reordered) == root {
		t.Fatal("leaf order must matter")
	}
}

func TestBuildProofOutOfRange(t *testing.T) {
	if _, err := BuildProof([]crypto.Hash{leaf("a")}, 1); err == nil {
		t.Fatal("out-of-range index must error")
	}
	if _, err := BuildProof(nil, 0); err == nil {
		t.Fatal("empty tree proof must error")
	}
}
