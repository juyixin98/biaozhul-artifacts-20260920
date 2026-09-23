// Package merkle implements a binary Merkle tree over 32-byte leaves using
// SHA-256, with Bitcoin-style duplication of a lone odd node. Inclusion
// proofs are genuinely computed and verified on both the producer and
// verifier side.
package merkle

import (
	"crypto/sha256"
	"errors"
	"fmt"

	"inbox/internal/crypto"
)

// EmptyRoot is the root of a tree with no leaves.
var EmptyRoot = crypto.Hash(sha256.Sum256([]byte("inbox/merkle/empty/v1")))

// ProofSide indicates where the sibling sits relative to the verified node.
type ProofSide uint8

const (
	Left  ProofSide = 0 // sibling is the left child
	Right ProofSide = 1 // sibling is the right child
)

// ProofStep is one level of a Merkle inclusion proof.
type ProofStep struct {
	Sibling crypto.Hash
	Side    ProofSide
}

// Proof is a Merkle inclusion proof.
type Proof struct {
	Index int
	Steps []ProofStep
}

func parentHash(left, right crypto.Hash) crypto.Hash {
	var buf [65]byte
	buf[0] = 0x01 // domain prefix prevents leaf/internal node confusion
	copy(buf[1:33], left[:])
	copy(buf[33:], right[:])
	return sha256.Sum256(buf[:])
}

// Root computes the Merkle root of the leaves. The order of leaves is the
// canonical message order within a block.
func Root(leaves []crypto.Hash) crypto.Hash {
	if len(leaves) == 0 {
		return EmptyRoot
	}
	level := make([]crypto.Hash, len(leaves))
	copy(level, leaves)
	for len(level) > 1 {
		next := make([]crypto.Hash, 0, (len(level)+1)/2)
		for i := 0; i < len(level); i += 2 {
			if i+1 == len(level) {
				// Lone odd node is promoted by duplicating it.
				next = append(next, parentHash(level[i], level[i]))
			} else {
				next = append(next, parentHash(level[i], level[i+1]))
			}
		}
		level = next
	}
	return level[0]
}

// BuildProof constructs an inclusion proof for leaves[index].
func BuildProof(leaves []crypto.Hash, index int) (Proof, error) {
	if index < 0 || index >= len(leaves) {
		return Proof{}, fmt.Errorf("leaf index %d out of range (n=%d)", index, len(leaves))
	}
	if len(leaves) == 1 {
		return Proof{Index: index, Steps: nil}, nil
	}
	p := Proof{Index: index}
	level := make([]crypto.Hash, len(leaves))
	copy(level, leaves)
	idx := index
	for len(level) > 1 {
		var siblingIdx int
		var side ProofSide
		if idx%2 == 0 {
			siblingIdx = idx + 1
			side = Right
			if siblingIdx == len(level) {
				// Duplicated lone node: sibling is itself.
				siblingIdx = idx
			}
		} else {
			siblingIdx = idx - 1
			side = Left
		}
		p.Steps = append(p.Steps, ProofStep{Sibling: level[siblingIdx], Side: side})
		next := make([]crypto.Hash, 0, (len(level)+1)/2)
		for i := 0; i < len(level); i += 2 {
			if i+1 == len(level) {
				next = append(next, parentHash(level[i], level[i]))
			} else {
				next = append(next, parentHash(level[i], level[i+1]))
			}
		}
		level = next
		idx /= 2
	}
	return p, nil
}

// Verify checks that leaf is included under root per the given proof.
func Verify(root, leaf crypto.Hash, p Proof) error {
	h := leaf
	for _, step := range p.Steps {
		switch step.Side {
		case Left:
			h = parentHash(step.Sibling, h)
		case Right:
			h = parentHash(h, step.Sibling)
		default:
			return errors.New("proof contains invalid side marker")
		}
	}
	if h != root {
		return errors.New("merkle proof does not match block message root")
	}
	return nil
}
