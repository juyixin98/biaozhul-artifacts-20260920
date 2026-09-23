// Package chain implements the hash-chain data structure used to verify
// synchronized blocks. Every block commits to its predecessor via a SHA-256
// hash over a length-prefixed canonical encoding, so tampering with any block
// in a contiguous prefix invalidates the link of every following block.
package chain

import (
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"fmt"
	"math"
)

// GenesisHeight is the height of the genesis block (height zero).
const GenesisHeight int64 = 0

// HashLen is the length of a SHA-256 hash in bytes.
const HashLen = sha256.Size

var zeroParent = make([]byte, HashLen)

// Block is one element of the chain.
type Block struct {
	Height     int64
	ParentHash []byte
	Hash       []byte
	Timestamp  int64
	Body       []byte
}

// Genesis builds the height-0 block from a seed. Its parent hash is all zeroes.
func Genesis(seed []byte) Block {
	parent := append([]byte(nil), zeroParent...)
	body := append([]byte(nil), seed...)
	return Block{
		Height:     GenesisHeight,
		ParentHash: parent,
		Hash:       HashFor(GenesisHeight, parent, 0, body),
		Timestamp:  0,
		Body:       body,
	}
}

// appendBytes encodes a length-prefixed byte slice into dst.
func appendBytes(dst, b []byte) []byte {
	var lenbuf [4]byte
	binary.BigEndian.PutUint32(lenbuf[:], uint32(len(b)))
	dst = append(dst, lenbuf[:]...)
	return append(dst, b...)
}

// Canonical is the deterministic byte representation that gets hashed.
// All integers are big-endian and byte slices are 4-byte length-prefixed so
// encodings cannot be ambiguous.
func Canonical(height int64, parentHash []byte, timestamp int64, body []byte) []byte {
	buf := make([]byte, 0, 8+HashLen+8+4+len(body))
	var num [8]byte
	binary.BigEndian.PutUint64(num[:], uint64(height))
	buf = append(buf, num[:]...)
	buf = appendBytes(buf, parentHash)
	binary.BigEndian.PutUint64(num[:], uint64(timestamp))
	buf = append(buf, num[:]...)
	buf = appendBytes(buf, body)
	return buf
}

// HashFor computes the block hash for the given fields (real SHA-256).
func HashFor(height int64, parentHash []byte, timestamp int64, body []byte) []byte {
	sum := sha256.Sum256(Canonical(height, parentHash, timestamp, body))
	return sum[:]
}

// Child derives the next block after parent at height parent.Height+1.
func Child(parent Block, timestamp int64, body []byte) Block {
	height := parent.Height + 1
	if height == math.MaxInt64 {
		panic("chain: height overflow")
	}
	h := HashFor(height, parent.Hash, timestamp, body)
	return Block{
		Height:     height,
		ParentHash: append([]byte(nil), parent.Hash...),
		Hash:       h,
		Timestamp:  timestamp,
		Body:       append([]byte(nil), body...),
	}
}

// Rehash recomputes and overwrites Hash from the block's other fields.
// It is primarily useful for tests that need to forge internally-consistent
// blocks (e.g. simulating a divergent chain) and for canonicalizing input.
func (b *Block) Rehash() {
	b.Hash = HashFor(b.Height, b.ParentHash, b.Timestamp, b.Body)
}

// Clone returns a deep copy of the block.
func (b Block) Clone() Block {
	return Block{
		Height:     b.Height,
		ParentHash: append([]byte(nil), b.ParentHash...),
		Hash:       append([]byte(nil), b.Hash...),
		Timestamp:  b.Timestamp,
		Body:       append([]byte(nil), b.Body...),
	}
}

// Validation errors.
var (
	ErrEmptySegment    = errors.New("chain: empty segment")
	ErrNotGenesis      = errors.New("chain: first block is not genesis at height 0")
	ErrGenesisMismatch = errors.New("chain: genesis hash mismatch")
	ErrHeightGap       = errors.New("chain: non-consecutive heights")
	ErrParentMismatch  = errors.New("chain: parent hash mismatch")
	ErrHashMismatch    = errors.New("chain: block hash mismatch (data tampered)")
	ErrNilHash         = errors.New("chain: missing block hash")
)

// VerifyGenesis checks a single genesis block against the trusted hash.
func VerifyGenesis(genesisHash []byte, b Block) error {
	if b.Height != GenesisHeight {
		return fmt.Errorf("%w: got height %d", ErrNotGenesis, b.Height)
	}
	if len(b.Hash) == 0 {
		return ErrNilHash
	}
	want := HashFor(b.Height, b.ParentHash, b.Timestamp, b.Body)
	if !equalBytes(want, b.Hash) {
		return ErrHashMismatch
	}
	if !equalBytes(genesisHash, b.Hash) {
		return ErrGenesisMismatch
	}
	return nil
}

// VerifyChain verifies a segment that begins with the genesis block against a
// trusted genesis hash, checking recomputed hashes and parent links.
func VerifyChain(trustedGenesis []byte, blocks []Block) error {
	if len(blocks) == 0 {
		return ErrEmptySegment
	}
	if err := VerifyGenesis(trustedGenesis, blocks[0]); err != nil {
		return err
	}
	return verifySuffix(blocks)
}

// VerifyAppend verifies that blocks form a consecutive continuation whose first
// block links to parentHash at expectedHeight. It does NOT trust the blocks'
// declared hashes; it recomputes every hash from the payload.
func VerifyAppend(parentHash []byte, expectedHeight int64, blocks []Block) error {
	if len(blocks) == 0 {
		return ErrEmptySegment
	}
	prev := parentHash
	height := expectedHeight
	for i := range blocks {
		b := &blocks[i]
		if b.Height != height {
			return fmt.Errorf("%w: want height %d got %d", ErrHeightGap, height, b.Height)
		}
		if len(b.Hash) == 0 {
			return fmt.Errorf("%w at height %d", ErrNilHash, height)
		}
		want := HashFor(b.Height, b.ParentHash, b.Timestamp, b.Body)
		if !equalBytes(want, b.Hash) {
			return fmt.Errorf("%w at height %d", ErrHashMismatch, height)
		}
		if !equalBytes(b.ParentHash, prev) {
			return fmt.Errorf("%w at height %d", ErrParentMismatch, height)
		}
		prev = b.Hash
		height++
	}
	return nil
}

// VerifyInternal checks a segment in isolation: heights must be consecutive,
// every self-hash must recompute from the payload, and each block after the
// first must link to the previous block's hash. It does NOT verify that the
// first block's parent is the trusted tip — that boundary check requires the
// prefix to already be verified and is performed by VerifyAppend once the
// segment becomes contiguous. A self-consistent fork therefore passes here
// and is only rejected at the boundary, which is exactly why an unverified
// anchor must never be trusted.
func VerifyInternal(blocks []Block) error {
	if len(blocks) == 0 {
		return ErrEmptySegment
	}
	height := blocks[0].Height
	prev := blocks[0].ParentHash
	first := true
	for i := range blocks {
		b := &blocks[i]
		if !first {
			if b.Height != height {
				return fmt.Errorf("%w: want height %d got %d", ErrHeightGap, height, b.Height)
			}
		} else if b.Height < 0 {
			return fmt.Errorf("%w: negative height %d", ErrHeightGap, b.Height)
		}
		if len(b.Hash) == 0 {
			return fmt.Errorf("%w at height %d", ErrNilHash, height)
		}
		want := HashFor(b.Height, b.ParentHash, b.Timestamp, b.Body)
		if !equalBytes(want, b.Hash) {
			return fmt.Errorf("%w at height %d", ErrHashMismatch, height)
		}
		if !first && !equalBytes(b.ParentHash, prev) {
			return fmt.Errorf("%w at height %d", ErrParentMismatch, height)
		}
		prev = b.Hash
		height = b.Height + 1
		first = false
	}
	return nil
}

// verifySuffix checks parent links and hashes of blocks[1:] assuming blocks[0]
// was already validated.
func verifySuffix(blocks []Block) error {
	prev := blocks[0].Hash
	height := blocks[0].Height + 1
	for i := 1; i < len(blocks); i++ {
		b := &blocks[i]
		if b.Height != height {
			return fmt.Errorf("%w: want height %d got %d", ErrHeightGap, height, b.Height)
		}
		if len(b.Hash) == 0 {
			return fmt.Errorf("%w at height %d", ErrNilHash, height)
		}
		want := HashFor(b.Height, b.ParentHash, b.Timestamp, b.Body)
		if !equalBytes(want, b.Hash) {
			return fmt.Errorf("%w at height %d", ErrHashMismatch, height)
		}
		if !equalBytes(b.ParentHash, prev) {
			return fmt.Errorf("%w at height %d", ErrParentMismatch, height)
		}
		prev = b.Hash
		height++
	}
	return nil
}

func equalBytes(a, b []byte) bool {
	if len(a) != len(b) {
		return false
	}
	// Constant-time comparison; hashes are equal length but guard anyway.
	var diff byte
	for i := range a {
		diff |= a[i] ^ b[i]
	}
	return diff == 0
}
