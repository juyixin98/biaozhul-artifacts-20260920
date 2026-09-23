// Package gen deterministically builds the canonical hash chain that the stub
// nodes serve and that is recorded as the trusted reference sample.
package gen

import (
	"crypto/sha256"
	"encoding/binary"
	"fmt"

	"nodesync/internal/chain"
)

// Spec describes a chain to generate.
type Spec struct {
	Length    int    // number of non-genesis blocks (tip height == Length)
	Seed      []byte // genesis seed
	BodyNonce []byte // mixed into every body so test fixtures differ
}

// Build returns length+1 blocks (heights 0..Length), fully linked and hashed.
func Build(s Spec) []chain.Block {
	blocks := make([]chain.Block, 0, s.Length+1)
	g := chain.Genesis(s.Seed)
	blocks = append(blocks, g)
	parent := g
	for h := int64(1); h <= int64(s.Length); h++ {
		b := chain.Child(parent, 1_700_000_000+h, BodyFor(h, s.BodyNonce))
		blocks = append(blocks, b)
		parent = b
	}
	return blocks
}

// BodyFor is the deterministic body of height h.
func BodyFor(h int64, nonce []byte) []byte {
	var hb [8]byte
	binary.BigEndian.PutUint64(hb[:], uint64(h))
	buf := make([]byte, 0, len("nodesync-body:")+len(nonce)+8)
	buf = append(buf, "nodesync-body:"...)
	buf = append(buf, nonce...)
	buf = append(buf, hb[:]...)
	mix := sha256.Sum256(buf)
	return []byte(fmt.Sprintf("h=%d:%x", h, mix[:8]))
}
