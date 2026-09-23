// Package replay independently re-executes committed blocks and cross-checks
// the resulting state roots against three sources of truth:
//
//  1. the state root stored in each block header;
//  2. the state reconstructed by the snapshot+delta query path;
//  3. the state root recorded inside each snapshot manifest.
//
// Any divergence is returned as a Mismatch. Verification requires full
// delta availability from the starting point; pruned histories are
// reported as an error rather than silently skipped.
package replay

import (
	"encoding/json"
	"fmt"

	"github.com/example/snapshotprune/internal/crypto"
	"github.com/example/snapshotprune/internal/snapshot"
	"github.com/example/snapshotprune/internal/state"
	"github.com/example/snapshotprune/internal/store"
	"github.com/example/snapshotprune/internal/types"
)

// Mismatch describes one cross-check divergence.
type Mismatch struct {
	Height uint64 `json:"height"`
	Kind   string `json:"kind"` // header | tx_root | signature | query_path | snapshot_manifest
	Want   string `json:"want,omitempty"`
	Got    string `json:"got,omitempty"`
	Detail string `json:"detail,omitempty"`
}

// Result is the verification report.
type Result struct {
	FromHeight  uint64     `json:"from_height"`
	ToHeight    uint64     `json:"to_height"`
	Blocks      int        `json:"blocks_checked"`
	Matches     bool       `json:"matches"`
	SnapshotsOK int        `json:"snapshots_checked"`
	Mismatches  []Mismatch `json:"mismatches,omitempty"`
}

// QueryReconstructor abstracts the engine's snapshot+delta query path so
// replay does not import engine (avoiding an import cycle).
type QueryReconstructor interface {
	// Reconstruct returns the table at height using the live query path.
	Reconstruct(height uint64) (state.Table, error)
}

// Checker runs verifications.
type Checker struct {
	st    *store.Store
	mgr   *snapshot.Manager
	query QueryReconstructor
}

// NewChecker builds a Checker. query may be nil to skip query-path checks.
func NewChecker(st *store.Store, mgr *snapshot.Manager, query QueryReconstructor) *Checker {
	return &Checker{st: st, mgr: mgr, query: query}
}

// GenesisTable decodes the genesis allocations into a state table.
func GenesisTable(st *store.Store) (state.Table, error) {
	b, err := st.GetMeta("genesis")
	if err != nil {
		return nil, err
	}
	var g types.Genesis
	if err := json.Unmarshal(b, &g); err != nil {
		return nil, err
	}
	tbl := state.Table{}
	for _, a := range g.Allocations {
		addr, err := types.ParseAddress(a.Address)
		if err != nil {
			return nil, err
		}
		tbl[addr] = &types.Account{Balance: a.Balance}
	}
	return tbl, nil
}

// VerifyFromScratch replays blocks 1..tip independently, checking every
// signature, tx Merkle root and state root, plus snapshot manifests and
// the live query path at snapshot heights.
func (c *Checker) VerifyFromScratch() (*Result, error) {
	tip, err := c.st.Tip()
	if err != nil {
		return nil, err
	}
	tbl, err := GenesisTable(c.st)
	if err != nil {
		return nil, err
	}
	snapHeights := map[uint64]bool{}
	if c.mgr != nil {
		for _, info := range c.mgr.List() {
			snapHeights[info.Height] = true
		}
	}

	res := &Result{FromHeight: 1, ToHeight: tip, Matches: true}
	for h := uint64(1); h <= tip; h++ {
		blk, err := c.st.Block(h)
		if err != nil {
			return nil, fmt.Errorf("read block %d: %w", h, err)
		}
		for i := range blk.Txs {
			if err := crypto.CheckTx(&blk.Txs[i]); err != nil {
				res.Matches = false
				res.Mismatches = append(res.Mismatches, Mismatch{
					Height: h, Kind: "signature", Detail: err.Error(),
				})
			}
		}
		if tr := types.TxRoot(blk.Txs); tr != blk.Header.TxRoot {
			res.Matches = false
			res.Mismatches = append(res.Mismatches, Mismatch{
				Height: h, Kind: "tx_root",
				Want: blk.Header.TxRoot.Hex(), Got: tr.Hex(),
			})
		}
		if _, err := state.Apply(tbl, h, blk.Txs); err != nil {
			return nil, fmt.Errorf("re-execute block %d: %w", h, err)
		}
		got := state.Root(tbl)
		if got != blk.Header.StateRoot {
			res.Matches = false
			res.Mismatches = append(res.Mismatches, Mismatch{
				Height: h, Kind: "header",
				Want: blk.Header.StateRoot.Hex(), Got: got.Hex(),
				Detail: "re-executed state root differs from block header",
			})
		}

		if snapHeights[h] {
			// Check the query path (snapshot + deltas) agrees.
			if c.query != nil {
				qt, err := c.query.Reconstruct(h)
				if err != nil {
					res.Matches = false
					res.Mismatches = append(res.Mismatches, Mismatch{
						Height: h, Kind: "query_path", Detail: err.Error(),
					})
				} else if qr := state.Root(qt); qr != got {
					res.Matches = false
					res.Mismatches = append(res.Mismatches, Mismatch{
						Height: h, Kind: "query_path",
						Want: got.Hex(), Got: qr.Hex(),
					})
				}
			}
			// Check the independently loaded snapshot file agrees.
			if c.mgr != nil {
				st2, err := c.mgr.Load(h)
				if err != nil {
					res.Matches = false
					res.Mismatches = append(res.Mismatches, Mismatch{
						Height: h, Kind: "snapshot_manifest", Detail: err.Error(),
					})
				} else if sr := state.Root(st2); sr != got {
					res.Matches = false
					res.Mismatches = append(res.Mismatches, Mismatch{
						Height: h, Kind: "snapshot_manifest",
						Want: got.Hex(), Got: sr.Hex(),
					})
				} else {
					res.SnapshotsOK++
				}
			}
		}
		res.Blocks++
	}
	return res, nil
}
