package indexer

import (
	"context"
	"errors"
	"fmt"
	"time"

	"forkindexer/internal/model"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// readTx runs fn inside a REPEATABLE READ transaction: every query sees one
// consistent snapshot. Even if a reorg commits concurrently, the reader gets
// either the complete pre-reorg view or the complete post-reorg view, never a
// mix of the two. A snapshot invalidated between first-query and commit
// (SQLSTATE 40001) is retried with a fresh snapshot.
func (ix *Indexer) readTx(ctx context.Context, fn func(pgx.Tx) error) error {
	const maxAttempts = 10
	for attempt := 0; attempt < maxAttempts; attempt++ {
		err := ix.readTxOnce(ctx, fn)
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && pgErr.Code == "40001" {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(time.Duration(attempt+1) * time.Millisecond):
			}
			continue
		}
		return err
	}
	return fmt.Errorf("read snapshot did not settle after %d attempts", maxAttempts)
}

func (ix *Indexer) readTxOnce(ctx context.Context, fn func(pgx.Tx) error) error {
	tx, err := ix.pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead})
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	if err := fn(tx); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// Head returns the canonical chain tip, or nil when no canonical chain exists.
func (ix *Indexer) Head(ctx context.Context) (*HeadInfo, error) {
	var h HeadInfo
	err := ix.readTx(ctx, func(tx pgx.Tx) error {
		var hash *string
		if err := tx.QueryRow(ctx, "SELECT head_hash FROM chain_state WHERE id = 1").Scan(&hash); err != nil {
			return err
		}
		if hash == nil {
			return pgx.ErrNoRows
		}
		var height int64
		if err := tx.QueryRow(ctx, "SELECT height FROM blocks WHERE hash = $1", *hash).Scan(&height); err != nil {
			return err
		}
		h = HeadInfo{Hash: *hash, Height: height}
		return nil
	})
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &h, nil
}

// State returns head plus the durable ingestion cursor.
func (ix *Indexer) State(ctx context.Context) (*StateInfo, error) {
	var s StateInfo
	err := ix.readTx(ctx, func(tx pgx.Tx) error {
		var head *string
		if err := tx.QueryRow(ctx,
			"SELECT head_hash, ingest_seq, stream_offset FROM chain_state WHERE id = 1",
		).Scan(&head, &s.IngestSeq, &s.StreamOffset); err != nil {
			return err
		}
		if head != nil {
			s.HeadHash = *head
			if err := tx.QueryRow(ctx, "SELECT height FROM blocks WHERE hash = $1", *head).Scan(&s.HeadHeight); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return &s, nil
}

// GetBlock loads any stored block by hash. Canonical reflects membership in
// the current canonical chain. Returns nil when unknown.
func (ix *Indexer) GetBlock(ctx context.Context, hash string) (*BlockInfo, error) {
	var out *BlockInfo
	err := ix.readTx(ctx, func(tx pgx.Tx) error {
		b, err := loadBlock(ctx, tx, hash)
		if err != nil {
			return err
		}
		var canon bool
		if err := tx.QueryRow(ctx,
			"SELECT EXISTS(SELECT 1 FROM canonical_blocks WHERE hash = $1)", b.Hash,
		).Scan(&canon); err != nil {
			return err
		}
		out = &BlockInfo{
			Hash: b.Hash, ParentHash: b.ParentHash, Height: b.Height,
			Status: b.Status, Canonical: canon, ReceivedSeq: b.ReceivedSeq,
			Transactions: b.Transactions,
		}
		return nil
	})
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return out, err
}

// BlockAtHeight returns the canonical block at a height, or nil.
func (ix *Indexer) BlockAtHeight(ctx context.Context, height int64) (*BlockInfo, error) {
	var hash string
	err := ix.readTx(ctx, func(tx pgx.Tx) error {
		return tx.QueryRow(ctx,
			"SELECT hash FROM canonical_blocks WHERE height = $1", height).Scan(&hash)
	})
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return ix.GetBlock(ctx, hash)
}

// Chain returns the canonical blocks from the head back to genesis, or from a
// given hash (which must be canonical) back to genesis.
func (ix *Indexer) Chain(ctx context.Context, fromHash string) ([]*BlockInfo, error) {
	var out []*BlockInfo
	err := ix.readTx(ctx, func(tx pgx.Tx) error {
		var start string
		if fromHash != "" {
			var ok bool
			if err := tx.QueryRow(ctx,
				"SELECT EXISTS(SELECT 1 FROM canonical_blocks WHERE hash = $1)", fromHash).Scan(&ok); err != nil {
				return err
			}
			if !ok {
				return ErrNotCanonical
			}
			start = fromHash
		} else {
			var head *string
			if err := tx.QueryRow(ctx, "SELECT head_hash FROM chain_state WHERE id = 1").Scan(&head); err != nil {
				return err
			}
			if head == nil {
				return nil
			}
			start = *head
		}

		rows, err := tx.Query(ctx, `
			WITH RECURSIVE chain(hash) AS (
				SELECT $1
				UNION ALL
				SELECT b.parent_hash FROM blocks b JOIN chain c ON b.hash = c.hash
				WHERE b.parent_hash <> $2
			)
			SELECT `+blockCols+` FROM blocks WHERE hash IN (SELECT hash FROM chain)
			ORDER BY height DESC`, start, model.GenesisParent)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			b, err := scanBlock(rows)
			if err != nil {
				return err
			}
			out = append(out, &BlockInfo{
				Hash: b.Hash, ParentHash: b.ParentHash, Height: b.Height,
				Status: b.Status, Canonical: true, ReceivedSeq: b.ReceivedSeq,
				Transactions: b.Transactions,
			})
		}
		return rows.Err()
	})
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}
	return out, err
}

// ConsistencyProbe reads head, canonical-chain depth and one balance in a
// SINGLE repeatable-read snapshot. It is the "no mixed reads during reorg"
// invariant check: within one snapshot the chain rooted at head_hash always
// contains exactly head_height+1 blocks and balances match that chain.
type ConsistencySnapshot struct {
	HeadHash   string
	HeadHeight int64
	Depth      int
	Balance    string
}

func (ix *Indexer) ConsistencyProbe(ctx context.Context, address string) (*ConsistencySnapshot, error) {
	var out ConsistencySnapshot
	out.Balance = "0"
	err := ix.readTx(ctx, func(tx pgx.Tx) error {
		var head *string
		if err := tx.QueryRow(ctx, "SELECT head_hash FROM chain_state WHERE id = 1").Scan(&head); err != nil {
			return err
		}
		if head == nil {
			return nil
		}
		out.HeadHash = *head
		if err := tx.QueryRow(ctx, "SELECT height FROM blocks WHERE hash = $1", *head).Scan(&out.HeadHeight); err != nil {
			return err
		}
		if err := tx.QueryRow(ctx, `
			WITH RECURSIVE chain(hash) AS (
				SELECT $1
				UNION ALL
				SELECT b.parent_hash FROM blocks b JOIN chain c ON b.hash = c.hash
				WHERE b.parent_hash <> $2
			)
			SELECT count(*) FROM chain`, *head, model.GenesisParent).Scan(&out.Depth); err != nil {
			return err
		}
		// Canonical membership count must agree too (head vs canonical_blocks
		// are rewritten in the same writer transaction).
		var canonCount int
		if err := tx.QueryRow(ctx, "SELECT count(*) FROM canonical_blocks").Scan(&canonCount); err != nil {
			return err
		}
		if canonCount != out.Depth {
			return fmt.Errorf("snapshot torn: recursive depth %d but canonical_blocks rows %d", out.Depth, canonCount)
		}
		if err := tx.QueryRow(ctx, "SELECT amount FROM balances WHERE address = $1", address).Scan(&out.Balance); err != nil {
			if err != pgx.ErrNoRows {
				return err
			}
			out.Balance = "0"
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return &out, nil
}

// Balance returns the spendable balance of an address on the canonical chain.
// Unknown accounts have balance 0.
func (ix *Indexer) Balance(ctx context.Context, address string) (string, error) {
	var amount string
	err := ix.readTx(ctx, func(tx pgx.Tx) error {
		return tx.QueryRow(ctx, "SELECT amount FROM balances WHERE address = $1", address).Scan(&amount)
	})
	if errors.Is(err, pgx.ErrNoRows) {
		return "0", nil
	}
	if err != nil {
		return "", err
	}
	return amount, nil
}

// AccountTransactions lists canonical transfers involving address (as sender
// or receiver), in chain order with a limit.
func (ix *Indexer) AccountTransactions(ctx context.Context, address string, limit int) ([]TransactionRow, error) {
	if limit <= 0 || limit > 1000 {
		limit = 100
	}
	var out []TransactionRow
	err := ix.readTx(ctx, func(tx pgx.Tx) error {
		rows, err := tx.Query(ctx, `
			SELECT block_hash, height, tx_index, from_addr, to_addr, amount
			FROM canonical_transactions
			WHERE from_addr = $1 OR to_addr = $1
			ORDER BY height ASC, tx_index ASC
			LIMIT $2`, address, limit)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var r TransactionRow
			if err := rows.Scan(&r.BlockHash, &r.Height, &r.TxIndex, &r.From, &r.To, &r.Amount); err != nil {
				return err
			}
			out = append(out, r)
		}
		return rows.Err()
	})
	return out, err
}
