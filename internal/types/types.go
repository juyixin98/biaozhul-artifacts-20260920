// Package types defines the wire/domain types and their canonical binary
// encodings. Everything that is hashed or signed goes through fixed-width
// big-endian encodings so that signatures and roots are unambiguous.
package types

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
)

// Address is a 20-byte account identifier derived from a public key.
type Address [20]byte

// Hash is a 32-byte SHA-256 digest.
type Hash [32]byte

// ZeroHash is the all-zero parent hash used by the genesis block.
var ZeroHash Hash

func decodeFixed(s string, n int) ([]byte, error) {
	if len(s) >= 2 && s[:2] == "0x" {
		s = s[2:]
	}
	b, err := hex.DecodeString(s)
	if err != nil {
		return nil, fmt.Errorf("invalid hex %q: %w", s, err)
	}
	if len(b) != n {
		return nil, fmt.Errorf("invalid hex %q: expected %d bytes, got %d", s, n, len(b))
	}
	return b, nil
}

func (a Address) Hex() string { return "0x" + hex.EncodeToString(a[:]) }

func (h Hash) Hex() string { return "0x" + hex.EncodeToString(h[:]) }

func (h Hash) String() string { return h.Hex() }

func (a Address) String() string { return a.Hex() }

// MarshalJSON / UnmarshalJSON make addresses human readable on the wire.
func (a Address) MarshalJSON() ([]byte, error) {
	return json.Marshal(a.Hex())
}

func (a *Address) UnmarshalJSON(data []byte) error {
	var s string
	if err := json.Unmarshal(data, &s); err != nil {
		return err
	}
	b, err := decodeFixed(s, 20)
	if err != nil {
		return err
	}
	copy(a[:], b)
	return nil
}

func (h Hash) MarshalJSON() ([]byte, error) { return json.Marshal(h.Hex()) }

func (h *Hash) UnmarshalJSON(data []byte) error {
	var s string
	if err := json.Unmarshal(data, &s); err != nil {
		return err
	}
	b, err := decodeFixed(s, 32)
	if err != nil {
		return err
	}
	copy(h[:], b)
	return nil
}

// ParseAddress parses a 0x-prefixed 20-byte address.
func ParseAddress(s string) (Address, error) {
	var a Address
	b, err := decodeFixed(s, 20)
	if err != nil {
		return a, err
	}
	copy(a[:], b)
	return a, nil
}

// ParseHash parses a 0x-prefixed 32-byte hash.
func ParseHash(s string) (Hash, error) {
	var h Hash
	b, err := decodeFixed(s, 32)
	if err != nil {
		return h, err
	}
	copy(h[:], b)
	return h, nil
}

// Account is the mutable on-chain state of one account.
type Account struct {
	Nonce   uint64 `json:"nonce"`
	Balance uint64 `json:"balance"`
}

// Op is a single account change recorded in a block's delta.
// Before == nil means the account did not exist before the block.
// After  == nil means the account was removed by the block.
type Op struct {
	Account Address  `json:"account"`
	Before  *Account `json:"before"`
	After   *Account `json:"after"`
}

// Delta holds every account change applied at one height. Re-applying the
// ops in order reproduces the post-block state from the pre-block state.
type Delta struct {
	Height uint64 `json:"height"`
	Ops    []*Op  `json:"ops"`
}

// Tx is a signed value transfer. Fees are burned.
type Tx struct {
	Nonce  uint64  `json:"nonce"`
	From   Address `json:"from"`
	To     Address `json:"to"`
	Amount uint64  `json:"amount"`
	Fee    uint64  `json:"fee"`
	PubKey []byte  `json:"pubkey"` // 32-byte Ed25519 public key
	Sig    []byte  `json:"sig"`    // 64-byte Ed25519 signature
}

// SigningBytes is the 64-byte canonical message signed by the sender.
// Layout: nonce(8) || from(20) || to(20) || amount(8) || fee(8).
func (tx *Tx) SigningBytes() []byte {
	buf := make([]byte, 64)
	binary.BigEndian.PutUint64(buf[0:8], tx.Nonce)
	copy(buf[8:28], tx.From[:])
	copy(buf[28:48], tx.To[:])
	binary.BigEndian.PutUint64(buf[48:56], tx.Amount)
	binary.BigEndian.PutUint64(buf[56:64], tx.Fee)
	return buf
}

// FullBytes binds the public key and signature into the transaction leaf
// used by the block Merkle root: signing(64) || pubkey(32) || sig(64).
func (tx *Tx) FullBytes() []byte {
	buf := make([]byte, 160)
	copy(buf[0:64], tx.SigningBytes())
	copy(buf[64:96], tx.PubKey)
	copy(buf[96:160], tx.Sig)
	return buf
}

// Header is the fixed-width block header committed by BlockHash.
type Header struct {
	Height     uint64 `json:"height"`
	ParentHash Hash   `json:"parent_hash"`
	StateRoot  Hash   `json:"state_root"`
	TxRoot     Hash   `json:"tx_root"`
	Timestamp  int64  `json:"timestamp_unix_nano"`
}

// HeaderBytes is the 112-byte canonical header encoding.
func HeaderBytes(h *Header) []byte {
	buf := make([]byte, 112)
	binary.BigEndian.PutUint64(buf[0:8], h.Height)
	copy(buf[8:40], h.ParentHash[:])
	copy(buf[40:72], h.StateRoot[:])
	copy(buf[72:104], h.TxRoot[:])
	binary.BigEndian.PutUint64(buf[104:112], uint64(h.Timestamp))
	return buf
}

// BlockHash commits the whole header (which itself commits state/tx roots).
func BlockHash(h *Header) Hash {
	return sha256.Sum256(HeaderBytes(h))
}

// Block is a sequenced batch of signed transactions.
type Block struct {
	Header Header `json:"header"`
	Txs    []Tx   `json:"txs"`
}

// Hash returns the block hash.
func (b *Block) Hash() Hash { return BlockHash(&b.Header) }

// TxRoot computes the binary SHA-256 Merkle root of the transactions.
// An empty block has root SHA-256(""); odd nodes pair with their last copy.
func TxRoot(txs []Tx) Hash {
	if len(txs) == 0 {
		return sha256.Sum256(nil)
	}
	level := make([][32]byte, len(txs))
	for i := range txs {
		level[i] = sha256.Sum256(txs[i].FullBytes())
	}
	for len(level) > 1 {
		next := make([][32]byte, 0, (len(level)+1)/2)
		for i := 0; i < len(level); i += 2 {
			pair := make([]byte, 64)
			copy(pair[0:32], level[i][:])
			if i+1 < len(level) {
				copy(pair[32:64], level[i+1][:])
			} else {
				copy(pair[32:64], level[i][:])
			}
			next = append(next, sha256.Sum256(pair))
		}
		level = next
	}
	return level[0]
}

// GenesisAlloc funds one address at genesis.
type GenesisAlloc struct {
	Address string `json:"address"`
	Balance uint64 `json:"balance"`
}

// Genesis is the chain initialization configuration.
type Genesis struct {
	ChainID     string         `json:"chain_id"`
	Allocations []GenesisAlloc `json:"allocations"`
}

// ErrBlockExists and friends are sentinel validation errors.
var (
	ErrBadNonce = errors.New("invalid transaction nonce")
)
