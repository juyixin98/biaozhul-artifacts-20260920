package indexer

import (
	"context"
	"encoding/json"
	"fmt"
	"math/big"
	"sort"

	"forkindexer/internal/model"

	"github.com/jackc/pgx/v5"
)

// RebuildReport is the result of independently reconstructing the ledger from
// every stored block starting at genesis.
type RebuildReport struct {
	CanonicalHashes []string          `json:"canonicalHashes"`
	HeadHash        string            `json:"headHash"`
	Balances        map[string]string `json:"balances"`
	Mismatches      []string          `json:"mismatches"`
	Match           bool              `json:"match"`
}

// VerifyAgainstRebuild ignores the maintained canonical_*/balances tables and
// replays every stored block from genesis in pure Go using the same rules
// (structural connectivity, cumulative weight = chain length, hash tie-break,
// overdraft invalidation), then compares the recomputed chain and balances to
// the incrementally maintained database state.
//
// This is the acceptance oracle: after arbitrary delivery orders, duplicate
// deliveries and crashes, the incremental result must equal a clean rebuild.
func (ix *Indexer) VerifyAgainstRebuild(ctx context.Context) (*RebuildReport, error) {
	type row struct {
		hash, parent string
		height       int64
		seq          int64
		txs          []model.Transfer
	}
	var all []row
	byHash := map[string]*row{}
	if err := ix.readTx(ctx, func(tx pgx.Tx) error {
		qr, err := tx.Query(ctx, `
			SELECT hash, parent_hash, height, received_seq, transactions
			FROM blocks
			ORDER BY received_seq`)
		if err != nil {
			return err
		}
		defer qr.Close()
		for qr.Next() {
			var r row
			var raw []byte
			if err := qr.Scan(&r.hash, &r.parent, &r.height, &r.seq, &raw); err != nil {
				return err
			}
			if err := json.Unmarshal(raw, &r.txs); err != nil {
				return err
			}
			all = append(all, r)
		}
		return qr.Err()
	}); err != nil {
		return nil, err
	}
	for i := range all {
		byHash[all[i].hash] = &all[i]
	}

	// Structural statuses as a fixpoint.
	status := map[string]string{}
	for _, r := range all {
		if r.parent == model.GenesisParent && r.height == 0 {
			status[r.hash] = model.StatusConnected
		} else {
			status[r.hash] = model.StatusStaged
		}
	}
	propagate := func() {
	forLoop:
		for {
			for _, r := range all {
				if status[r.hash] != model.StatusStaged {
					continue
				}
				p := byHash[r.parent]
				switch {
				case p == nil:
					// remains staged
				case status[p.hash] == model.StatusConnected && p.height == r.height-1:
					status[r.hash] = model.StatusConnected
					continue forLoop
				case status[p.hash] == model.StatusInvalid:
					status[r.hash] = model.StatusInvalid
					continue forLoop
				case status[p.hash] == model.StatusConnected && p.height != r.height-1:
					status[r.hash] = model.StatusInvalid
					continue forLoop
				}
			}
			return
		}
	}
	propagate()

	chainOf := func(tip string) []string {
		var path []string
		cur := tip
		for cur != model.GenesisParent {
			r, ok := byHash[cur]
			if !ok {
				break
			}
			path = append(path, cur)
			cur = r.parent
		}
		// reverse -> oldest first
		for i, j := 0, len(path)-1; i < j; i, j = i+1, j-1 {
			path[i], path[j] = path[j], path[i]
		}
		return path
	}

	bestTip := func() string {
		hasChild := map[string]bool{}
		for _, r := range all {
			if status[r.hash] == model.StatusConnected {
				hasChild[r.parent] = true
			}
		}
		tip := ""
		for _, r := range all {
			if status[r.hash] != model.StatusConnected || hasChild[r.hash] {
				continue
			}
			if tip == "" || r.height > byHash[tip].height ||
				(r.height == byHash[tip].height && r.hash < tip) {
				tip = r.hash
			}
		}
		return tip
	}

	invalidateSub := func(hash string) {
		var walk func(h string)
		walk = func(h string) {
			if status[h] == model.StatusInvalid {
				return
			}
			status[h] = model.StatusInvalid
			for _, r := range all {
				if r.parent == h {
					walk(r.hash)
				}
			}
		}
		walk(hash)
	}

	// Repeatedly take the globally best tip and replay its whole chain from
	// genesis; overdrafting tips invalidate their subtree, exactly as online.
	var canonical []string
	var bals map[string]*big.Int
	for {
		tip := bestTip()
		if tip == "" {
			break
		}
		path := chainOf(tip)
		try := map[string]*big.Int{}
		overdraftAt := ""
		for _, h := range path {
			r := byHash[h]
			bad := false
			for _, t := range r.txs {
				amt, _ := new(big.Int).SetString(t.Amount, 10)
				if t.From != "" {
					cur, ok := try[t.From]
					if !ok {
						cur = new(big.Int)
					}
					if cur.Cmp(amt) < 0 {
						bad = true
						break
					}
					try[t.From] = new(big.Int).Sub(cur, amt)
				}
				if t.To != "" {
					cur, ok := try[t.To]
					if !ok {
						cur = new(big.Int)
					}
					try[t.To] = new(big.Int).Add(cur, amt)
				}
			}
			if bad {
				overdraftAt = h
				break
			}
		}
		if overdraftAt != "" {
			invalidateSub(overdraftAt)
			propagate()
			continue
		}
		canonical = path
		bals = try
		break
	}

	report := &RebuildReport{Match: true, Balances: map[string]string{}}
	if len(canonical) > 0 {
		report.HeadHash = canonical[len(canonical)-1] // chain order: tip is last
	}
	sortedCanonical := append([]string(nil), canonical...)
	sort.Strings(sortedCanonical)
	report.CanonicalHashes = sortedCanonical
	for addr, v := range bals {
		report.Balances[addr] = v.String()
	}

	// Compare against database-maintained state.
	dbCanonical := map[string]bool{}
	dbBalances := map[string]string{}
	var dbHead *string
	if err := ix.readTx(ctx, func(tx pgx.Tx) error {
		rows, err := tx.Query(ctx, "SELECT hash FROM canonical_blocks")
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var h string
			if err := rows.Scan(&h); err != nil {
				return err
			}
			dbCanonical[h] = true
		}
		if err := rows.Err(); err != nil {
			return err
		}
		rows2, err := tx.Query(ctx, "SELECT address, amount FROM balances")
		if err != nil {
			return err
		}
		defer rows2.Close()
		for rows2.Next() {
			var a, amt string
			if err := rows2.Scan(&a, &amt); err != nil {
				return err
			}
			dbBalances[a] = amt
		}
		if err := rows2.Err(); err != nil {
			return err
		}
		return tx.QueryRow(ctx, "SELECT head_hash FROM chain_state WHERE id = 1").Scan(&dbHead)
	}); err != nil {
		return nil, err
	}

	rebuiltSet := map[string]bool{}
	for _, h := range canonical {
		rebuiltSet[h] = true
	}
	for h := range rebuiltSet {
		if !dbCanonical[h] {
			report.Mismatches = append(report.Mismatches, fmt.Sprintf("canonical set: block %s rebuilt as canonical but not in database", h))
		}
	}
	for h := range dbCanonical {
		if !rebuiltSet[h] {
			report.Mismatches = append(report.Mismatches, fmt.Sprintf("canonical set: database marks %s canonical but rebuild does not", h))
		}
	}
	for addr, amt := range report.Balances {
		if dbBalances[addr] != amt {
			report.Mismatches = append(report.Mismatches, fmt.Sprintf("balance %s: rebuilt %s vs database %q", addr, amt, dbBalances[addr]))
		}
	}
	for addr, amt := range dbBalances {
		if got, ok := report.Balances[addr]; !ok || got != amt {
			report.Mismatches = append(report.Mismatches, fmt.Sprintf("balance %s: database %s not matched by rebuild (%q)", addr, amt, got))
		}
	}
	dbHeadStr := ""
	if dbHead != nil {
		dbHeadStr = *dbHead
	}
	if dbHeadStr != report.HeadHash {
		report.Mismatches = append(report.Mismatches, fmt.Sprintf("head: rebuilt %s vs database %q", report.HeadHash, dbHeadStr))
	}
	report.Match = len(report.Mismatches) == 0
	sort.Strings(report.Mismatches)
	return report, nil
}
