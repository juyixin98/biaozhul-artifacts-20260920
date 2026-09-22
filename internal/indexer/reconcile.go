package indexer

import (
	"context"
	"fmt"
	"math/big"

	"forkindexer/internal/model"

	"github.com/jackc/pgx/v5"
)

// reconcile selects the best valid chain tip and switches the canonical chain
// to it inside the caller's transaction.
//
// Weight rule: cumulative weight = chain length = tip height + 1 (every block
// weighs 1). On equal weight the tip whose hash is lexicographically smaller
// wins. The switch itself is: load balances, undo old-exclusive path in
// reverse block order, apply new-exclusive path in forward order, rewrite
// canonical_* tables, flush balances. A candidate that overdrafts is marked
// invalid (with all descendants) and selection restarts.
//
// It returns a non-nil ReorgSummary only when the head actually changed.
func (ix *Indexer) reconcile(ctx context.Context, tx pgx.Tx, currentHead *string) (*ReorgSummary, error) {
	for {
		tip, err := bestTip(ctx, tx)
		if err != nil {
			return nil, err
		}

		switch {
		case tip == "" && currentHead == nil:
			return nil, nil // no connected chain anywhere
		case tip == "":
			// Defensive: canonical head exists but is not among valid tips.
			return nil, fmt.Errorf("internal: no valid tip while canonical head %s exists", *currentHead)
		case currentHead == nil:
			// First canonical chain.
		case tip == *currentHead:
			return nil, nil
		}

		var tipHeight int64
		if err := tx.QueryRow(ctx, "SELECT height FROM blocks WHERE hash = $1", tip).Scan(&tipHeight); err != nil {
			return nil, err
		}

		var oldHeight int64
		hasOld := false
		if currentHead != nil {
			if err := tx.QueryRow(ctx, "SELECT height FROM blocks WHERE hash = $1", *currentHead).Scan(&oldHeight); err != nil {
				if err != pgx.ErrNoRows {
					return nil, err
				}
			} else {
				hasOld = true
			}
		}

		switch {
		case hasOld && oldHeight > tipHeight:
			return nil, nil // current chain is heavier
		case hasOld && oldHeight == tipHeight && *currentHead < tip:
			return nil, nil // equal weight, current head wins the hash tie-break
		}

		// New tip wins. Split paths at the common ancestor.
		oldExclusive, newExclusive, err := splitPaths(ctx, tx, currentHead, tip)
		if err != nil {
			return nil, err
		}
		// Snapshot all balances that either path touches.
		addrs := touchedAddresses(ctx, tx, oldExclusive, newExclusive)
		bals, err := loadBalances(ctx, tx, addrs)
		if err != nil {
			return nil, err
		}

		// Everything below can be rolled back to here when the candidate
		// overdrafts, without discarding the advisory lock / inserts.
		if _, err := tx.Exec(ctx, "SAVEPOINT apply_candidate"); err != nil {
			return nil, err
		}

		// Undo the old-exclusive path newest -> oldest.
		for i := len(oldExclusive) - 1; i >= 0; i-- {
			blk, err := loadBlock(ctx, tx, oldExclusive[i])
			if err != nil {
				return nil, err
			}
			if err := applyTransfers(bals, blk.Transactions, true); err != nil {
				rollbackCandidate(ctx, tx)
				return nil, fmt.Errorf("undoing canonical block %s failed: %w", blk.Hash, err)
			}
			if _, err := tx.Exec(ctx, "DELETE FROM canonical_blocks WHERE hash = $1", blk.Hash); err != nil {
				rollbackCandidate(ctx, tx)
				return nil, err
			}
			if _, err := tx.Exec(ctx, "DELETE FROM canonical_transactions WHERE block_hash = $1", blk.Hash); err != nil {
				rollbackCandidate(ctx, tx)
				return nil, err
			}
		}

		// Apply the new-exclusive path oldest -> newest.
		var overdraft *string
		for _, h := range newExclusive {
			blk, err := loadBlock(ctx, tx, h)
			if err != nil {
				rollbackCandidate(ctx, tx)
				return nil, err
			}
			if err := applyTransfers(bals, blk.Transactions, false); err != nil {
				over := h
				overdraft = &over
				break
			}
			if err := writeCanonicalBlock(ctx, tx, *blk); err != nil {
				rollbackCandidate(ctx, tx)
				return nil, err
			}
		}

		if overdraft != nil {
			// Candidate chain cannot be funded. The offending block and every
			// block descending from it are invalid forever; retry selection.
			rollbackCandidate(ctx, tx)
			if err := invalidateSubtree(ctx, tx, *overdraft); err != nil {
				return nil, err
			}
			if err := ix.fixBlockStatuses(ctx, tx); err != nil {
				return nil, err
			}
			continue
		}

		if err := flushBalances(ctx, tx, bals); err != nil {
			rollbackCandidate(ctx, tx)
			return nil, err
		}
		if _, err := tx.Exec(ctx, "RELEASE SAVEPOINT apply_candidate"); err != nil {
			return nil, err
		}

		rec := &ReorgSummary{
			ToHead:  tip,
			Applied: append([]string(nil), newExclusive...),
			Undone:  append([]string(nil), oldExclusive...),
		}
		if currentHead != nil {
			rec.FromHead = *currentHead
		}
		newHead := tip
		currentHead = &newHead
		return rec, nil
	}
}

func rollbackCandidate(ctx context.Context, tx pgx.Tx) {
	_, _ = tx.Exec(ctx, "ROLLBACK TO SAVEPOINT apply_candidate")
}

// bestTip returns the hash of the best chain tip among connected blocks that
// no other connected block extends: greatest height, then smallest hash.
func bestTip(ctx context.Context, tx pgx.Tx) (string, error) {
	var tip string
	err := tx.QueryRow(ctx, `
		SELECT b.hash
		FROM blocks b
		WHERE b.status = 'connected'
		  AND NOT EXISTS (
			SELECT 1 FROM blocks c
			WHERE c.parent_hash = b.hash AND c.status = 'connected')
		ORDER BY b.height DESC, b.hash ASC
		LIMIT 1`).Scan(&tip)
	if err == pgx.ErrNoRows {
		return "", nil
	}
	return tip, err
}

// splitPaths returns the old and new exclusive block paths (oldest -> newest),
// stopping at their common ancestor. When oldHead is nil the old path is empty
// (pure extension from nothing = first chain).
func splitPaths(ctx context.Context, tx pgx.Tx, oldHead *string, newHead string) (oldEx, newEx []string, err error) {
	chain := func(head string) ([]string, map[string]bool, error) {
		// Walk tip -> genesis by following parent_hash as a block reference;
		// the genesis block (whose parent_hash is the zero sentinel) is
		// included, after which recursion finds no parent and stops.
		rows, err := tx.Query(ctx, `
			WITH RECURSIVE chain(hash) AS (
				SELECT $1
				UNION ALL
				SELECT b.parent_hash
				FROM blocks b JOIN chain c ON b.hash = c.hash
				WHERE b.parent_hash <> $2
			)
			SELECT hash FROM chain`, head, model.GenesisParent)
		if err != nil {
			return nil, nil, err
		}
		defer rows.Close()
		var path []string
		set := map[string]bool{}
		for rows.Next() {
			var h string
			if err := rows.Scan(&h); err != nil {
				return nil, nil, err
			}
			path = append(path, h)
			set[h] = true
		}
		return path, set, rows.Err()
	}

	newPath, newSet, err := chain(newHead)
	if err != nil {
		return nil, nil, err
	}
	if oldHead == nil {
		return nil, reverseCopy(newPath), nil
	}
	oldPath, oldSet, err := chain(*oldHead)
	if err != nil {
		return nil, nil, err
	}
	// Walk from each tip toward genesis until paths meet.
	common := ""
	for _, h := range oldPath { // oldPath starts at the tip
		if newSet[h] {
			common = h
			break
		}
	}
	if common == "" {
		// Two unrelated genesis chains: undo everything old, apply all new.
		return reverseCopy(oldPath), reverseCopy(newPath), nil
	}

	for _, h := range oldPath {
		if h == common {
			break
		}
		oldEx = append(oldEx, h)
	}
	for _, h := range newPath {
		if h == common {
			break
		}
		newEx = append(newEx, h)
	}
	_ = oldSet
	return reverseCopy(oldEx), reverseCopy(newEx), nil
}

func reverseCopy(in []string) []string {
	out := make([]string, len(in))
	for i, h := range in {
		out[len(in)-1-i] = h
	}
	return out
}

func touchedAddresses(ctx context.Context, tx pgx.Tx, paths ...[]string) map[string]struct{} {
	set := map[string]struct{}{}
	for _, p := range paths {
		for _, h := range p {
			blk, err := loadBlock(ctx, tx, h)
			if err != nil {
				continue
			}
			for _, t := range blk.Transactions {
				if t.From != "" {
					set[t.From] = struct{}{}
				}
				if t.To != "" {
					set[t.To] = struct{}{}
				}
			}
		}
	}
	return set
}

func loadBalances(ctx context.Context, tx pgx.Tx, addrs map[string]struct{}) (map[string]*big.Int, error) {
	m := make(map[string]*big.Int, len(addrs))
	if len(addrs) == 0 {
		return m, nil
	}
	rows, err := tx.Query(ctx, "SELECT address, amount FROM balances WHERE address = ANY($1)", addrList(addrs))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var addr, amt string
		if err := rows.Scan(&addr, &amt); err != nil {
			return nil, err
		}
		v, ok := new(big.Int).SetString(amt, 10)
		if !ok {
			return nil, fmt.Errorf("corrupt balance for %s: %q", addr, amt)
		}
		m[addr] = v
	}
	return m, rows.Err()
}

func addrList(set map[string]struct{}) []string {
	out := make([]string, 0, len(set))
	for a := range set {
		out = append(out, a)
	}
	return out
}

// applyTransfers mutates bals for one block. undo=true reverses it. The
// canonical ledger always has funds, so undo cannot overdraft; apply can.
func applyTransfers(bals map[string]*big.Int, txs []model.Transfer, undo bool) error {
	type delta struct {
		addr string
		neg  bool
		amt  *big.Int
	}
	deltas := make([]delta, 0, len(txs)*2)
	for _, t := range txs {
		amt, ok := new(big.Int).SetString(t.Amount, 10)
		if !ok {
			return fmt.Errorf("bad amount %q", t.Amount)
		}
		if undo {
			if t.To != "" {
				deltas = append(deltas, delta{t.To, true, amt}) // credit back
			}
			if t.From != "" {
				deltas = append(deltas, delta{t.From, false, amt}) // take back
			}
		} else {
			if t.From != "" {
				deltas = append(deltas, delta{t.From, true, amt})
			}
			if t.To != "" {
				deltas = append(deltas, delta{t.To, false, amt})
			}
		}
	}
	for _, d := range deltas {
		if d.neg {
			if !subBal(bals, d.addr, d.amt) {
				return fmt.Errorf("insufficient funds: account %s cannot move %s", d.addr, d.amt.String())
			}
		} else {
			addBal(bals, d.addr, d.amt)
		}
	}
	return nil
}

func writeCanonicalBlock(ctx context.Context, tx pgx.Tx, blk storedBlock) error {
	if _, err := tx.Exec(ctx,
		"INSERT INTO canonical_blocks (height, hash) VALUES ($1,$2)",
		blk.Height, blk.Hash); err != nil {
		return err
	}
	for i, t := range blk.Transactions {
		if _, err := tx.Exec(ctx, `
			INSERT INTO canonical_transactions (block_hash, height, tx_index, from_addr, to_addr, amount)
			VALUES ($1,$2,$3,$4,$5,$6)`,
			blk.Hash, blk.Height, i, t.From, t.To, t.Amount); err != nil {
			return err
		}
	}
	return nil
}

func flushBalances(ctx context.Context, tx pgx.Tx, bals map[string]*big.Int) error {
	for addr, v := range bals {
		if v.Sign() == 0 {
			// Returned-to-zero accounts are not stored, matching a genesis
			// rebuild, where a zero balance is simply an absent map entry.
			if _, err := tx.Exec(ctx, "DELETE FROM balances WHERE address = $1", addr); err != nil {
				return err
			}
			continue
		}
		if _, err := tx.Exec(ctx, `
			INSERT INTO balances (address, amount) VALUES ($1,$2)
			ON CONFLICT (address) DO UPDATE SET amount = EXCLUDED.amount`,
			addr, v.String()); err != nil {
			return err
		}
	}
	return nil
}

// invalidateSubtree marks a block and every block descending from it invalid.
func invalidateSubtree(ctx context.Context, tx pgx.Tx, hash string) error {
	_, err := tx.Exec(ctx, `
		WITH RECURSIVE sub(hash) AS (
			SELECT $1
			UNION
			SELECT b.hash FROM blocks b JOIN sub s ON b.parent_hash = s.hash
		)
		UPDATE blocks SET status = 'invalid'
		WHERE hash IN (SELECT hash FROM sub) AND status <> 'invalid'`, hash)
	return err
}
