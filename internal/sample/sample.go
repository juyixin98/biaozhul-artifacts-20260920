// Package sample defines the trusted reference chain ("golden sample") produced
// offline by cmd/genchain. The synchronizer treats this file — never any
// remote node's advertised height — as the source of truth for the genesis
// hash, target tip and the final chain digest.
package sample

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"

	"nodesync/internal/chain"
)

// DigestAlgorithm documents how the chain digest is computed.
const DigestAlgorithm = "SHA-256 over the concatenation of block hashes in ascending height order"

// BlockJSON is the on-disk representation of one block.
type BlockJSON struct {
	Height     int64  `json:"height"`
	ParentHash string `json:"parent_hash"`
	Hash       string `json:"hash"`
	Timestamp  int64  `json:"timestamp"`
	Body       string `json:"body"` // hex
}

// Sample is the trusted reference document.
type Sample struct {
	CreatedAt       string      `json:"created_at"`
	Seed            string      `json:"seed"`       // hex
	BodyNonce       string      `json:"body_nonce"` // hex
	Length          int         `json:"length"`
	GenesisHash     string      `json:"genesis_hash"`
	TipHeight       int64       `json:"tip_height"`
	TipHash         string      `json:"tip_hash"`
	DigestAlgorithm string      `json:"digest_algorithm"`
	Digest          string      `json:"digest"`
	Blocks          []BlockJSON `json:"blocks"`
}

// Digest computes the chain digest over block hashes in height order.
func Digest(blocks []chain.Block) []byte {
	h := sha256.New()
	for i := range blocks {
		h.Write(blocks[i].Hash)
	}
	return h.Sum(nil)
}

// FromBlocks builds the trusted sample for a fully built chain.
func FromBlocks(blocks []chain.Block, seed, nonce []byte, createdAt string) *Sample {
	s := &Sample{
		CreatedAt:       createdAt,
		Seed:            hex.EncodeToString(seed),
		BodyNonce:       hex.EncodeToString(nonce),
		Length:          len(blocks) - 1,
		GenesisHash:     hex.EncodeToString(blocks[0].Hash),
		TipHeight:       blocks[len(blocks)-1].Height,
		TipHash:         hex.EncodeToString(blocks[len(blocks)-1].Hash),
		DigestAlgorithm: DigestAlgorithm,
		Digest:          hex.EncodeToString(Digest(blocks)),
		Blocks:          make([]BlockJSON, len(blocks)),
	}
	for i := range blocks {
		b := blocks[i]
		s.Blocks[i] = BlockJSON{
			Height:     b.Height,
			ParentHash: hex.EncodeToString(b.ParentHash),
			Hash:       hex.EncodeToString(b.Hash),
			Timestamp:  b.Timestamp,
			Body:       hex.EncodeToString(b.Body),
		}
	}
	return s
}

// ToBlocks decodes the sample into domain blocks and re-verifies the whole
// chain internally, so a corrupted sample file is detected at load time.
func (s *Sample) ToBlocks() ([]chain.Block, error) {
	if len(s.Blocks) != s.Length+1 {
		return nil, fmt.Errorf("sample: block count %d does not match length %d", len(s.Blocks), s.Length)
	}
	blocks := make([]chain.Block, len(s.Blocks))
	for i, bj := range s.Blocks {
		ph, err := hex.DecodeString(bj.ParentHash)
		if err != nil {
			return nil, fmt.Errorf("sample: block %d parent_hash: %w", bj.Height, err)
		}
		hsh, err := hex.DecodeString(bj.Hash)
		if err != nil {
			return nil, fmt.Errorf("sample: block %d hash: %w", bj.Height, err)
		}
		body, err := hex.DecodeString(bj.Body)
		if err != nil {
			return nil, fmt.Errorf("sample: block %d body: %w", bj.Height, err)
		}
		blocks[i] = chain.Block{
			Height:     bj.Height,
			ParentHash: ph,
			Hash:       hsh,
			Timestamp:  bj.Timestamp,
			Body:       body,
		}
	}
	genesis, err := hex.DecodeString(s.GenesisHash)
	if err != nil {
		return nil, fmt.Errorf("sample: genesis_hash: %w", err)
	}
	if err := chain.VerifyChain(genesis, blocks); err != nil {
		return nil, fmt.Errorf("sample: reference chain failed verification: %w", err)
	}
	if got := hex.EncodeToString(Digest(blocks)); got != s.Digest {
		return nil, fmt.Errorf("sample: digest mismatch: file %s computed %s", s.Digest, got)
	}
	return blocks, nil
}

// GenesisHash decodes the trusted genesis hash.
func (s *Sample) GenesisHashBytes() ([]byte, error) {
	return hex.DecodeString(s.GenesisHash)
}

// Save writes the sample as indented JSON.
func (s *Sample) Save(path string) error {
	data, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, append(data, '\n'), 0o644)
}

// Load reads and parses a sample file.
func Load(path string) (*Sample, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var s Sample
	if err := json.Unmarshal(data, &s); err != nil {
		return nil, fmt.Errorf("sample: parse %s: %w", path, err)
	}
	return &s, nil
}
