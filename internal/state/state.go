// Package state applies transactions against an in-memory account table and
// computes the canonical state root. It is pure logic with no storage.
package state

import (
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"fmt"
	"sort"

	"github.com/example/snapshotprune/internal/types"
)

var (
	// ErrInsufficientBalance is returned when a tx spends more than held.
	ErrInsufficientBalance = errors.New("insufficient balance")
	// ErrBadNonce is returned when a tx nonce does not match the account.
	ErrBadNonce = errors.New("invalid transaction nonce")
	// ErrOverflow is returned when an addition would overflow uint64.
	ErrOverflow = errors.New("balance overflow")
)

// Table is the mutable full account state at a point in time.
type Table map[types.Address]*types.Account

// Clone returns a deep copy.
func (t Table) Clone() Table {
	out := make(Table, len(t))
	for a, acc := range t {
		cp := *acc
		out[a] = &cp
	}
	return out
}

// Summary is a compact digest returned to history queries.
type Summary struct {
	Height      uint64 `json:"height"`
	StateRoot   string `json:"state_root"`
	NumAccounts int    `json:"num_accounts"`
	TotalSupply uint64 `json:"total_supply"`
}

// AccountView is a state summary possibly focused on one account.
type AccountView struct {
	Summary Summary        `json:"summary"`
	Account *types.Account `json:"account,omitempty"`
	Address *types.Address `json:"address,omitempty"`
}

// Root computes the canonical state root: SHA-256 over the leaves
//
//	SHA-256(addr(20) || nonce(8) || balance(8))
//
// sorted by address, paired into a binary Merkle tree. An empty state
// (all genesis funds burned) has root SHA-256("").
func Root(t Table) types.Hash {
	if len(t) == 0 {
		return sha256.Sum256(nil)
	}
	addrs := make([]types.Address, 0, len(t))
	for a := range t {
		addrs = append(addrs, a)
	}
	sort.Slice(addrs, func(i, j int) bool {
		return addrs[i].Hex() < addrs[j].Hex()
	})
	level := make([][32]byte, len(addrs))
	for i, a := range addrs {
		var leaf [36]byte
		copy(leaf[0:20], a[:])
		binary.BigEndian.PutUint64(leaf[20:28], t[a].Nonce)
		binary.BigEndian.PutUint64(leaf[28:36], t[a].Balance)
		level[i] = sha256.Sum256(leaf[:])
	}
	for len(level) > 1 {
		next := make([][32]byte, 0, (len(level)+1)/2)
		for i := 0; i < len(level); i += 2 {
			var pair [64]byte
			copy(pair[0:32], level[i][:])
			if i+1 < len(level) {
				copy(pair[32:64], level[i+1][:])
			} else {
				copy(pair[32:64], level[i][:])
			}
			next = append(next, sha256.Sum256(pair[:]))
		}
		level = next
	}
	return level[0]
}

// TotalSupply sums all balances (uint64; chain genesis supply is bounded
// so this cannot overflow in practice).
func TotalSupply(t Table) uint64 {
	var sum uint64
	for _, acc := range t {
		sum += acc.Balance
	}
	return sum
}

// Describe returns the JSON digest for the table at height.
func Describe(t Table, height uint64) Summary {
	return Summary{
		Height:      height,
		StateRoot:   Root(t).Hex(),
		NumAccounts: len(t),
		TotalSupply: TotalSupply(t),
	}
}

// Apply executes a block's transactions in order against t and returns the
// delta ops describing every touched account (before/after images).
// The table is modified in place; callers wanting isolation must Clone.
func Apply(t Table, height uint64, txs []types.Tx) (*types.Delta, error) {
	delta := &types.Delta{Height: height}
	for i := range txs {
		tx := &txs[i]
		sender := t[tx.From]
		if sender == nil {
			return nil, fmt.Errorf("%w: sender %s does not exist", ErrInsufficientBalance, tx.From.Hex())
		}
		if tx.Nonce != sender.Nonce {
			return nil, fmt.Errorf("%w: account %s has nonce %d, tx uses %d",
				ErrBadNonce, tx.From.Hex(), sender.Nonce, tx.Nonce)
		}
		spent := tx.Amount + tx.Fee
		if spent < tx.Amount || spent < tx.Fee {
			return nil, fmt.Errorf("%w: spend amount overflow", ErrOverflow)
		}
		if sender.Balance < spent {
			return nil, fmt.Errorf("%w: account %s has %d, needs %d",
				ErrInsufficientBalance, tx.From.Hex(), sender.Balance, spent)
		}

		before := *sender
		sender.Balance -= spent
		sender.Nonce++
		// The fee is burned; recipient receives only Amount.
		if tx.To != tx.From {
			rcpt := t[tx.To]
			var beforeRcpt types.Account
			rcptExisted := rcpt != nil
			if rcpt == nil {
				rcpt = &types.Account{}
				t[tx.To] = rcpt
			} else {
				beforeRcpt = *rcpt
			}
			if rcpt.Balance+tx.Amount < rcpt.Balance {
				return nil, fmt.Errorf("%w: recipient %s balance overflow", ErrOverflow, tx.To.Hex())
			}
			rcpt.Balance += tx.Amount

			afterRcpt := *rcpt
			op := &types.Op{Account: tx.To}
			if rcptExisted {
				op.Before = &beforeRcpt
			}
			if !(rcpt.Balance == 0 && rcpt.Nonce == 0) {
				op.After = &afterRcpt
			}
			delta.Ops = append(delta.Ops, op)
		}
		after := *sender
		op := &types.Op{Account: tx.From, Before: &before}
		if !(sender.Balance == 0 && sender.Nonce == 0) {
			op.After = &after
		}
		delta.Ops = append(delta.Ops, op)
	}
	return delta, nil
}

// ApplyDelta re-applies a delta produced by Apply onto a snapshot table.
func ApplyDelta(t Table, d *types.Delta) {
	for _, op := range d.Ops {
		if op.After == nil {
			delete(t, op.Account)
		} else {
			cp := *op.After
			t[op.Account] = &cp
		}
	}
}
