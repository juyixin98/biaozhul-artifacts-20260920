// Package domain defines the agreed block/transfer model of the fork ledger
// indexer. This is deliberately NOT Ethereum: see README "Model contract".
package domain

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"strings"
)

// HashLen is the length in bytes of every block hash (SHA-256 digest).
const HashLen = 32

// ZeroHash is the parent hash of every genesis block (0x + 64 zeros).
const ZeroHash = "0x0000000000000000000000000000000000000000000000000000000000000000"

// Sentinel errors shared by store and HTTP layers.
var (
	ErrSameHashDifferentContent = errors.New("block with same hash but different content already exists")
	ErrHeightParentMismatch     = errors.New("block height is not parent height + 1")
	ErrGenesisParent            = errors.New("genesis block (height 0) must have the zero parent hash")
	ErrNonGenesisParent         = errors.New("non-genesis block must reference a non-zero parent hash")
	ErrDuplicateGenesis         = errors.New("a genesis block already exists; the model allows exactly one")
	ErrParentRejected           = errors.New("parent block was previously rejected")
	ErrNegativeAmount           = errors.New("transfer amount must be >= 0")
	ErrAmountOverflow           = errors.New("transfer amount overflows int64")
	ErrBalanceOverflow          = errors.New("balance update overflows int64")
	ErrInvalidAddress           = errors.New("address must be 0x followed by 40 lowercase hex digits")
	ErrInvalidHash              = errors.New("hash must be 0x followed by 64 hex digits")
	ErrHeightNegative           = errors.New("height must be >= 0")
	ErrHashMismatch             = errors.New("provided hash does not equal SHA-256 of the canonical block body")
	ErrSequenceGap              = errors.New("delivery sequence gap; cursor advanced past or skipping ahead")
	ErrSequenceTooSmall         = errors.New("delivery sequence already consumed")
)

// Transfer is one integer-value transfer inside a block.
//
// Amount is signed-carrying but must be non-negative per block; a debit is
// expressed by placing the account in "from". The model does not check
// sufficiency of funds (see README), so balances may be negative.
type Transfer struct {
	From   string `json:"from"`
	To     string `json:"to"`
	Amount int64  `json:"amount"`
}

// Block is the unit of the chain: hash, parent hash, height and a list of
// integer transfers. The hash is supplied by the producer and MUST equal
// SHA-256 over the canonical JSON body (see CanonicalBody / BlockHash).
type Block struct {
	Hash       string     `json:"hash"`
	ParentHash string     `json:"parent_hash"`
	Height     int64      `json:"height"`
	Transfers  []Transfer `json:"transfers"`

	// Raw is the exact canonical JSON this block was normalized from.
	// It is populated by Normalize and persisted so that "same hash,
	// different content" can be detected as raw-byte disagreement.
	Raw []byte `json:"-"`
}

// Envelope wraps one delivered block with its durable delivery sequence.
// Sequence numbers start at 1 and must be strictly increasing per stream;
// the committed cursor is the highest sequence durably applied.
type Envelope struct {
	Sequence int64 `json:"sequence"`
	Block    Block `json:"block"`
}

// canonicalBlock is the exact byte layout hashed by BlockHash. Field order is
// part of the protocol contract and must never change.
type canonicalBlock struct {
	ParentHash string              `json:"parent_hash"`
	Height     int64               `json:"height"`
	Transfers  []canonicalTransfer `json:"transfers"`
}

// canonicalTransfer fixes field order independently of Transfer's JSON tags.
type canonicalTransfer struct {
	From   string `json:"from"`
	To     string `json:"to"`
	Amount int64  `json:"amount"`
}

// ValidateHash checks the textual shape of a 0x-prefixed 32-byte hash and
// returns its lowercase form.
func ValidateHash(h string) (string, error) {
	if !strings.HasPrefix(h, "0x") || len(h) != 2+HashLen*2 {
		return "", ErrInvalidHash
	}
	low := strings.ToLower(h)
	if _, err := hex.DecodeString(low[2:]); err != nil {
		return "", ErrInvalidHash
	}
	return low, nil
}

// ValidateAddress checks the textual shape of an account address
// (0x + 40 lowercase hex digits) and returns the normalized form.
func ValidateAddress(a string) (string, error) {
	if !strings.HasPrefix(a, "0x") || len(a) != 42 {
		return "", fmt.Errorf("%w: %q", ErrInvalidAddress, a)
	}
	if a != strings.ToLower(a) {
		return "", fmt.Errorf("%w: %q (lowercase hex only)", ErrInvalidAddress, a)
	}
	if _, err := hex.DecodeString(a[2:]); err != nil {
		return "", fmt.Errorf("%w: %q", ErrInvalidAddress, a)
	}
	return a, nil
}

// CanonicalBody serializes the block body (everything except the declared
// hash) into the canonical byte layout used for hashing and storage.
func CanonicalBody(parentHash string, height int64, transfers []Transfer) ([]byte, error) {
	cts := make([]canonicalTransfer, len(transfers))
	for i, t := range transfers {
		from, err := ValidateAddress(t.From)
		if err != nil {
			return nil, err
		}
		to, err := ValidateAddress(t.To)
		if err != nil {
			return nil, err
		}
		if t.Amount < 0 {
			return nil, fmt.Errorf("%w: transfer %d", ErrNegativeAmount, i)
		}
		cts[i] = canonicalTransfer{From: from, To: to, Amount: t.Amount}
	}
	ph, err := ValidateHash(parentHash)
	if err != nil {
		return nil, err
	}
	if height < 0 {
		return nil, ErrHeightNegative
	}
	// No map keys anywhere: struct order is deterministic, no HTML escaping
	// needed (hex addresses/hashes contain no <, > or &), compact separators.
	return json.Marshal(canonicalBlock{
		ParentHash: ph,
		Height:     height,
		Transfers:  cts,
	})
}

// BlockHash returns SHA-256 over the canonical body, 0x-prefixed lowercase.
func BlockHash(raw []byte) string {
	sum := sha256.Sum256(raw)
	return "0x" + hex.EncodeToString(sum[:])
}

// Normalize validates, canonicalizes and hashes an incoming block.
//
// On success b.Raw holds the exact canonical bytes, b.Hash/b.ParentHash are
// lowercased, addresses are lowercased, and b.Hash equals BlockHash(b.Raw).
func Normalize(b *Block) error {
	raw, err := CanonicalBody(b.ParentHash, b.Height, b.Transfers)
	if err != nil {
		return err
	}
	want := BlockHash(raw)
	got, err := ValidateHash(b.Hash)
	if err != nil {
		return err
	}
	if got != want {
		return fmt.Errorf("%w: declared %s, computed %s", ErrHashMismatch, got, want)
	}
	b.Hash = want
	b.ParentHash, _ = ValidateHash(b.ParentHash)
	for i := range b.Transfers {
		b.Transfers[i].From, _ = ValidateAddress(b.Transfers[i].From)
		b.Transfers[i].To, _ = ValidateAddress(b.Transfers[i].To)
	}
	b.Raw = raw
	return nil
}

// IsGenesis reports whether the block sits at height 0 with the zero parent.
func (b *Block) IsGenesis() bool {
	return b.Height == 0 && b.ParentHash == ZeroHash
}

// ApplyTransfer adjusts balances for one transfer with checked int64
// arithmetic. Self-transfers are balance-neutral by construction
// (debit then credit the same key).
func ApplyTransfer(balances map[string]int64, t Transfer) error {
	if t.Amount < 0 {
		return ErrNegativeAmount
	}
	if t.From != t.To {
		fv, exists := balances[t.From]
		if !exists {
			fv = 0
		}
		if t.Amount > 0 && fv < math.MinInt64+t.Amount {
			return fmt.Errorf("%w: %s debit", ErrBalanceOverflow, t.From)
		}
		balances[t.From] = fv - t.Amount

		tv := balances[t.To]
		if t.Amount > 0 && tv > math.MaxInt64-t.Amount {
			return fmt.Errorf("%w: %s credit", ErrBalanceOverflow, t.To)
		}
		balances[t.To] = tv + t.Amount
	}
	return nil
}
