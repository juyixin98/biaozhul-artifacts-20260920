// Package model defines the agreed block/transfer model, its canonical
// serialization, real SHA-256 content hashing and input validation.
//
// This is an intentionally simplified ledger model. It is NOT Ethereum:
// there are no signatures, no EVM, no gas, no merkle tries. A block is a
// fixed shape (hash, parentHash, height, transactions) and a transaction is
// an integer value transfer between two addresses.
package model

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math/big"
	"regexp"
	"strings"
)

// GenesisParent is the sentinel parent hash of a height-0 block.
const GenesisParent = "0x0000000000000000000000000000000000000000000000000000000000000000"

// Block status as tracked by the indexer.
const (
	// StatusStaged: block stored but its parent chain back to genesis is incomplete.
	StatusStaged = "staged"
	// StatusConnected: structurally connected back to a genesis block.
	StatusConnected = "connected"
	// StatusInvalid: failed validation (bad height linkage) or overdrafted
	// when its tip attempted to become canonical; descendants inherit it.
	StatusInvalid = "invalid"
)

var (
	hashRe = regexp.MustCompile(`^0x[0-9a-f]{64}$`)
	addrRe = regexp.MustCompile(`^0x[0-9a-f]{40}$`)
)

// Transfer is one integer value transfer. An empty From mints new coins out
// of nothing; an empty To burns them.
type Transfer struct {
	From   string `json:"from"`
	To     string `json:"to"`
	Amount string `json:"amount"` // canonical decimal integer string, >= 0
}

// Block is the wire/storage shape. Hash is optional on input: when supplied it
// is checked against the real hash of the canonical preimage.
type Block struct {
	Hash         string     `json:"hash,omitempty"`
	ParentHash   string     `json:"parentHash"`
	Height       int64      `json:"height"`
	Transactions []Transfer `json:"transactions"`
}

// preimageTransfer serializes with keys in lexicographic order:
// amount, from, to. Declaration order is what encoding/json emits.
type preimageTransfer struct {
	Amount string `json:"amount"`
	From   string `json:"from"`
	To     string `json:"to"`
}

// preimageBlock serializes with keys in lexicographic order:
// height, parentHash, transactions.
type preimageBlock struct {
	Height       int64              `json:"height"`
	ParentHash   string             `json:"parentHash"`
	Transactions []preimageTransfer `json:"transactions"`
}

// ValidationError describes a malformed block.
type ValidationError struct{ Msg string }

func (e *ValidationError) Error() string { return e.Msg }

func validationError(format string, args ...any) error {
	return &ValidationError{Msg: fmt.Sprintf(format, args...)}
}

func isHash(s string) bool { return hashRe.MatchString(s) }
func isAddr(s string) bool { return s == "" || addrRe.MatchString(s) }

// Normalize validates a block, canonicalizes its fields, computes the real
// SHA-256 hash over the canonical JSON preimage and, when the caller supplied
// a hash, verifies it. It returns the normalized block and its preimage bytes.
// The preimage is what the store keeps so that "same hash, different content"
// can be detected on duplicate delivery.
func Normalize(in Block) (Block, []byte, error) {
	if in.Height < 0 {
		return Block{}, nil, validationError("height must be >= 0")
	}

	parent := strings.ToLower(strings.TrimSpace(in.ParentHash))
	if in.Height == 0 {
		// Both the empty string and the canonical zero hash denote "no parent".
		if parent != "" && parent != GenesisParent {
			return Block{}, nil, validationError("genesis block must have zero parent hash, got %q", parent)
		}
		parent = GenesisParent
	} else {
		if parent == "" || parent == GenesisParent {
			return Block{}, nil, validationError("block at height %d must name a non-zero parent", in.Height)
		}
		if !isHash(parent) {
			return Block{}, nil, validationError("parentHash is not a 0x-prefixed 32-byte hex hash: %q", parent)
		}
	}

	txs := make([]preimageTransfer, len(in.Transactions))
	outTxs := make([]Transfer, len(in.Transactions))
	for i, t := range in.Transactions {
		from := strings.ToLower(strings.TrimSpace(t.From))
		to := strings.ToLower(strings.TrimSpace(t.To))
		if !isAddr(from) {
			return Block{}, nil, validationError("transactions[%d].from is not a 0x-prefixed 20-byte hex address: %q", i, t.From)
		}
		if !isAddr(to) {
			return Block{}, nil, validationError("transactions[%d].to is not a 0x-prefixed 20-byte hex address: %q", i, t.To)
		}
		amount, ok := new(big.Int).SetString(strings.TrimSpace(t.Amount), 10)
		if !ok {
			return Block{}, nil, validationError("transactions[%d].amount is not a decimal integer: %q", i, t.Amount)
		}
		if amount.Sign() < 0 {
			return Block{}, nil, validationError("transactions[%d].amount must be >= 0, got %s", i, amount.String())
		}
		amt := amount.String() // canonical decimal form (no leading zeros, no + sign)
		txs[i] = preimageTransfer{Amount: amt, From: from, To: to}
		outTxs[i] = Transfer{From: from, To: to, Amount: amt}
	}

	pre := preimageBlock{Height: in.Height, ParentHash: parent, Transactions: txs}
	preimage, err := json.Marshal(pre)
	if err != nil {
		return Block{}, nil, fmt.Errorf("canonical preimage marshal: %w", err)
	}
	sum := sha256.Sum256(preimage)
	computed := "0x" + hex.EncodeToString(sum[:])

	want := strings.ToLower(strings.TrimSpace(in.Hash))
	if want != "" {
		if !isHash(want) {
			return Block{}, nil, validationError("hash is not a 0x-prefixed 32-byte hex hash: %q", in.Hash)
		}
		if want != computed {
			return Block{}, nil, validationError("hash %s does not match SHA-256 of canonical block contents %s", want, computed)
		}
	}

	out := Block{
		Hash:         computed,
		ParentHash:   parent,
		Height:       in.Height,
		Transactions: outTxs,
	}
	return out, preimage, nil
}

// MustHash normalizes and returns the block hash; it panics on invalid input.
// It exists for tests and fixture generators.
func MustHash(b Block) string {
	nb, _, err := Normalize(b)
	if err != nil {
		panic(err)
	}
	return nb.Hash
}

// ErrSameHashDifferentContent is returned when a known hash is re-delivered
// with different canonical contents.
var ErrSameHashDifferentContent = errors.New("block with same hash but different content rejected")
