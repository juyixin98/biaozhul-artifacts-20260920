// Package indexer implements the fork-aware ledger index:
// staging of orphan blocks, cumulative-weight chain selection with hash
// tie-break, atomic reorg bookkeeping and genesis rebuild verification.
package indexer

import (
	"context"
	"errors"
	"fmt"
	"math/big"
	"time"

	"forkindexer/internal/model"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

// advisoryLockKey serializes all writers within one database. Readers take
// snapshots and never block on it (MVCC), so a reorg can never be observed
// half-applied.
const advisoryLockKey int64 = 0x464C4944 // "FLID"

// errRetryableTx marks a transaction that should be replayed from scratch
// (couldn't take the advisory lock without waiting on a stale snapshot).
var errRetryableTx = errors.New("retryable transaction")

// BadBlockError marks a structurally invalid delivery (HTTP 400).
type BadBlockError struct {
	Index int
	Hash  string
	Err   error
}

func (e *BadBlockError) Error() string {
	return fmt.Sprintf("block[%d] %s: %v", e.Index, e.Hash, e.Err)
}
func (e *BadBlockError) Unwrap() error { return e.Err }

// HashConflictError is returned when a known hash arrives with different
// canonical contents (HTTP 409).
type HashConflictError struct{ Hash string }

func (e *HashConflictError) Error() string {
	return fmt.Sprintf("block %s already exists with different content", e.Hash)
}

// IngestedBlock reports the outcome for one delivered block.
type IngestedBlock struct {
	Hash      string `json:"hash"`
	Height    int64  `json:"height"`
	Status    string `json:"status"`
	Duplicate bool   `json:"duplicate"`
}

// ReorgSummary describes one canonical-chain switch.
type ReorgSummary struct {
	FromHead string   `json:"fromHead"`
	ToHead   string   `json:"toHead"`
	Undone   []string `json:"undone"`  // old exclusive path, old->new application order
	Applied  []string `json:"applied"` // new exclusive path, old->new application order
}

// IngestResult is the report of one ingest call.
type IngestResult struct {
	Accepted     int             `json:"accepted"`
	Duplicates   int             `json:"duplicates"`
	Blocks       []IngestedBlock `json:"blocks"`
	Reorg        *ReorgSummary   `json:"reorg,omitempty"`
	HeadHash     string          `json:"headHash"`
	HeadHeight   int64           `json:"headHeight"`
	IngestSeq    int64           `json:"ingestSeq"`
	StreamOffset int64           `json:"streamOffset"`
}

// Indexer is the fork-aware ledger index.
type Indexer struct {
	pool *pgxpool.Pool
}

func New(pool *pgxpool.Pool) *Indexer { return &Indexer{pool: pool} }

// Ingest validates and durably stores a batch of blocks, advances the cursor
// and reconciles the canonical chain. Block storage, ledger changes and the
// cursor are committed in ONE serializable transaction. Concurrent writers
// serialize through an advisory lock; when two batches still overlap at the
// snapshot level PostgreSQL raises SQLSTATE 40001, which is retried.
//
// streamOffset, when non-nil, advances the durable byte offset of the block
// stream the blocks were read from.
func (ix *Indexer) Ingest(ctx context.Context, in []model.Block, streamOffset *int64) (*IngestResult, error) {
	const maxAttempts = 20
	for attempt := 0; ; attempt++ {
		res, err := ix.ingestOnce(ctx, in, streamOffset)
		if err == nil {
			return res, nil
		}
		var pgErr *pgconn.PgError
		if (errors.As(err, &pgErr) && pgErr.Code == "40001") || errors.Is(err, errRetryableTx) {
			if attempt == maxAttempts-1 {
				return nil, fmt.Errorf("transaction did not settle after %d attempts: %w", maxAttempts, err)
			}
			// Brief backoff: another writer holds the chain lock or the
			// serialization graph is catching up.
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			case <-time.After(time.Duration(attempt+1) * 2 * time.Millisecond):
			}
			continue
		}
		return nil, err
	}
}

func (ix *Indexer) ingestOnce(ctx context.Context, in []model.Block, streamOffset *int64) (*IngestResult, error) {
	type accepted struct {
		block    model.Block
		preimage []byte
	}

	tx, err := ix.pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx) //nolint:errcheck

	// Try-lock, never enqueue behind the lock AFTER taking a snapshot: under
	// SERIALIZABLE a waiter would wake up with a stale snapshot and fail with
	// 40001 the instant it tried to update rows the lock-holder committed.
	// A failed try simply rolls back and is retried by Ingest.
	var acquired bool
	if err := tx.QueryRow(ctx, "SELECT pg_try_advisory_xact_lock($1)", advisoryLockKey).Scan(&acquired); err != nil {
		return nil, err
	}
	if !acquired {
		_ = tx.Rollback(ctx)
		return nil, errRetryableTx
	}

	var headHash *string
	var ingestSeq, curOffset int64
	if err := tx.QueryRow(ctx,
		"SELECT head_hash, ingest_seq, stream_offset FROM chain_state WHERE id = 1",
	).Scan(&headHash, &ingestSeq, &curOffset); err != nil {
		return nil, err
	}

	result := &IngestResult{}
	var fresh []accepted

	for i, raw := range in {
		b, preimage, err := model.Normalize(raw)
		if err != nil {
			return nil, &BadBlockError{Index: i, Hash: raw.Hash, Err: err}
		}

		var existing []byte
		err = tx.QueryRow(ctx, "SELECT preimage FROM blocks WHERE hash = $1", b.Hash).Scan(&existing)
		switch {
		case err == nil:
			if string(existing) != string(preimage) {
				return nil, &HashConflictError{Hash: b.Hash}
			}
			status := ix.blockStatus(ctx, tx, b.Hash)
			result.Blocks = append(result.Blocks, IngestedBlock{
				Hash: b.Hash, Height: b.Height, Status: status, Duplicate: true,
			})
			result.Duplicates++
			continue // duplicate delivery: never re-counted, never re-applied
		case errors.Is(err, pgx.ErrNoRows):
		default:
			return nil, err
		}

		if b.ParentHash != model.GenesisParent {
			var parentHeight int64
			err = tx.QueryRow(ctx, "SELECT height FROM blocks WHERE hash = $1", b.ParentHash).Scan(&parentHeight)
			switch {
			case err == nil:
				if parentHeight != b.Height-1 {
					return nil, &BadBlockError{Index: i, Hash: b.Hash, Err: fmt.Errorf(
						"height linkage broken: block height %d but parent %s has height %d",
						b.Height, b.ParentHash, parentHeight)}
				}
			case errors.Is(err, pgx.ErrNoRows):
				// Parent unknown: stage for now. It is validated again once the
				// parent arrives (and may then become invalid).
			default:
				return nil, err
			}
		}

		ingestSeq++
		initStatus := model.StatusStaged
		if b.ParentHash == model.GenesisParent {
			initStatus = model.StatusConnected
		}
		rawTxs, err := jsonMarshal(b.Transactions)
		if err != nil {
			return nil, err
		}
		if _, err := tx.Exec(ctx, `
			INSERT INTO blocks (hash, parent_hash, height, transactions, preimage, received_seq, status)
			VALUES ($1,$2,$3,$4,$5,$6,$7)`,
			b.Hash, b.ParentHash, b.Height, rawTxs, preimage, ingestSeq, initStatus,
		); err != nil {
			return nil, err
		}
		fresh = append(fresh, accepted{block: b, preimage: preimage})
		result.Blocks = append(result.Blocks, IngestedBlock{
			Hash: b.Hash, Height: b.Height, Status: initStatus,
		})
		result.Accepted++
	}

	// Propagate connectivity / invalidity across the whole block graph until
	// it reaches a fixed point.
	if err := ix.fixBlockStatuses(ctx, tx); err != nil {
		return nil, err
	}

	// Refresh statuses reported for this batch after propagation.
	for i := range result.Blocks {
		result.Blocks[i].Status = ix.blockStatus(ctx, tx, result.Blocks[i].Hash)
	}

	// Reconcile the canonical chain (may reorg; overdrafting candidates are
	// invalidated and the next best tip is tried).
	rec, err := ix.reconcile(ctx, tx, headHash)
	if err != nil {
		return nil, err
	}
	result.Reorg = rec
	if rec != nil {
		headHash = &rec.ToHead
	}

	newOffset := curOffset
	if streamOffset != nil && *streamOffset > newOffset {
		newOffset = *streamOffset
	}
	if _, err := tx.Exec(ctx,
		"UPDATE chain_state SET head_hash = $1, ingest_seq = $2, stream_offset = $3 WHERE id = 1",
		headHash, ingestSeq, newOffset,
	); err != nil {
		return nil, err
	}

	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}

	// Read final head info back for the report.
	h, err := ix.Head(ctx)
	if err != nil {
		return nil, err
	}
	if h != nil {
		result.HeadHash = h.Hash
		result.HeadHeight = h.Height
	}
	result.IngestSeq = ingestSeq
	result.StreamOffset = newOffset
	return result, nil
}

func (ix *Indexer) blockStatus(ctx context.Context, tx pgx.Tx, hash string) string {
	var status string
	if err := tx.QueryRow(ctx, "SELECT status FROM blocks WHERE hash = $1", hash).Scan(&status); err != nil {
		return model.StatusStaged
	}
	return status
}

// fixBlockStatuses runs connectivity and invalidation passes until no row
// changes anymore.
func (ix *Indexer) fixBlockStatuses(ctx context.Context, tx pgx.Tx) error {
	for {
		var changed int64

		// Connected = reachable from a genesis over parent links with the
		// height chain exactly -1 at every step.
		if _, err := tx.Exec(ctx, `
			WITH RECURSIVE conn(hash) AS (
				SELECT hash FROM blocks WHERE status = 'connected'
				UNION
				SELECT b.hash
				FROM blocks b
				JOIN conn c ON b.parent_hash = c.hash
				JOIN blocks p ON p.hash = b.parent_hash
				WHERE b.status = 'staged' AND p.height = b.height - 1
			)
			UPDATE blocks SET status = 'connected'
			WHERE hash IN (SELECT hash FROM conn) AND status = 'staged'`,
		); err != nil {
			return err
		}

		// A staged block whose connected parent has the wrong height is
		// structurally invalid forever.
		tag, err := tx.Exec(ctx, `
			UPDATE blocks b SET status = 'invalid'
			WHERE b.status = 'staged' AND EXISTS (
				SELECT 1 FROM blocks p
				WHERE p.hash = b.parent_hash AND p.status = 'connected'
				  AND p.height <> b.height - 1)`)
		if err != nil {
			return err
		}
		changed += tag.RowsAffected()

		// Invalidity is inherited by every descendant still staged.
		tag, err = tx.Exec(ctx, `
			WITH RECURSIVE bad(hash) AS (
				SELECT hash FROM blocks WHERE status = 'invalid'
				UNION
				SELECT b.hash FROM blocks b
				JOIN bad x ON b.parent_hash = x.hash
				WHERE b.status = 'staged'
			)
			UPDATE blocks SET status = 'invalid'
			WHERE hash IN (SELECT hash FROM bad) AND status = 'staged'`)
		if err != nil {
			return err
		}
		changed += tag.RowsAffected()

		if changed == 0 {
			return nil
		}
	}
}

// balance helpers shared by reconcile and rebuild -------------------------

func addBal(m map[string]*big.Int, addr string, v *big.Int) {
	if addr == "" {
		return // mint/burn touch no account
	}
	cur, ok := m[addr]
	if !ok {
		cur = new(big.Int)
		m[addr] = cur
	}
	cur.Add(cur, v)
}

func subBal(m map[string]*big.Int, addr string, v *big.Int) (ok bool) {
	if addr == "" {
		return true
	}
	cur, exists := m[addr]
	if !exists {
		cur = new(big.Int)
		m[addr] = cur
	}
	if cur.Cmp(v) < 0 {
		return false // overdraft
	}
	cur.Sub(cur, v)
	return true
}
