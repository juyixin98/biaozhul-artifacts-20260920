// Package pgstore implements the fork ledger state in PostgreSQL.
//
// All state transitions (block insertion, staging, connection, chain
// reorganization, balances and delivery cursor) happen inside a single
// transaction per delivery, so crash/restart never leaves cursor and
// balances inconsistent.
package pgstore

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"

	"forkindexer/internal/domain"

	// Registers the "pgx" database/sql driver.
	_ "github.com/jackc/pgx/v5/stdlib"
)

// SchemaDDL is applied idempotently on startup and tests.
const SchemaDDL = `
CREATE TABLE IF NOT EXISTS blocks (
    hash        TEXT PRIMARY KEY,
    parent_hash TEXT NOT NULL,
    height      BIGINT NOT NULL,
    cum_weight  BIGINT NOT NULL DEFAULT 0,
    status      TEXT NOT NULL CHECK (status IN ('staged','connected','rejected')),
    in_chain    BOOLEAN NOT NULL DEFAULT FALSE,
    raw         BYTEA NOT NULL,
    first_seq   BIGINT
);
CREATE INDEX IF NOT EXISTS blocks_parent_idx ON blocks(parent_hash);
CREATE INDEX IF NOT EXISTS blocks_status_idx ON blocks(status);

CREATE TABLE IF NOT EXISTS transfers (
    block_hash TEXT NOT NULL REFERENCES blocks(hash) ON DELETE CASCADE,
    idx        INTEGER NOT NULL,
    from_addr  TEXT NOT NULL,
    to_addr    TEXT NOT NULL,
    amount     BIGINT NOT NULL CHECK (amount >= 0),
    PRIMARY KEY (block_hash, idx)
);
CREATE INDEX IF NOT EXISTS transfers_from_idx ON transfers(from_addr);
CREATE INDEX IF NOT EXISTS transfers_to_idx   ON transfers(to_addr);

CREATE TABLE IF NOT EXISTS balances (
    address TEXT PRIMARY KEY,
    amount  BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    sequence   BIGINT PRIMARY KEY,
    block_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    id         INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    head       TEXT,
    cursor_seq BIGINT NOT NULL DEFAULT 0
);
INSERT INTO meta (id, head, cursor_seq)
VALUES (1, NULL, 0)
ON CONFLICT (id) DO NOTHING;
`

// Store wraps a database/sql connection pool backed by pgx.
type Store struct {
	db *sql.DB
}

// New opens the pool with the pgx stdlib driver and verifies connectivity.
func New(ctx context.Context, dsn string) (*Store, error) {
	connStr, err := normalizeDSN(dsn)
	if err != nil {
		return nil, err
	}
	db, err := sql.Open("pgx", connStr)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(16)
	if err := db.PingContext(ctx); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("ping postgres: %w", err)
	}
	return &Store{db: db}, nil
}

// Close releases the pool.
func (s *Store) Close() error { return s.db.Close() }

// DB exposes the pool for the migration command.
func (s *Store) DB() *sql.DB { return s.db }

// Ping checks database connectivity.
func (s *Store) Ping(ctx context.Context) error { return s.db.PingContext(ctx) }

// Migrate applies the schema idempotently.
func (s *Store) Migrate(ctx context.Context) error {
	_, err := s.db.ExecContext(ctx, SchemaDDL)
	return err
}

// normalizeDSN accepts either a postgres:// URL or a keyword DSN and returns
// a string the pgx stdlib driver understands as-is.
func normalizeDSN(dsn string) (string, error) {
	dsn = strings.TrimSpace(dsn)
	if dsn == "" {
		return "", errors.New("empty database DSN")
	}
	return dsn, nil
}

// IngestResult summarizes one block delivery.
type IngestResult struct {
	Hash        string `json:"hash"`
	Known       bool   `json:"already_known"` // hash was present before this delivery
	BlockStatus string `json:"block_status"`  // staged | connected | rejected
	HeadBefore  string `json:"head_before,omitempty"`
	HeadAfter   string `json:"head_after,omitempty"`
	Reorg       bool   `json:"reorg"`
	Cursor      int64  `json:"cursor"`
}

// IngestEnvelope delivers a single block at an explicit monotonically
// increasing sequence. seq == nil means an ad-hoc delivery that does not
// move the cursor (used by file ingest of bare blocks).
func (s *Store) IngestEnvelope(ctx context.Context, b *domain.Block, seq *int64) (IngestResult, error) {
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
	if err != nil {
		return IngestResult{}, err
	}
	defer func() { _ = tx.Rollback() }()

	res, err := ingestInTx(ctx, tx, b, seq)
	if err != nil {
		return IngestResult{}, err
	}
	if err := tx.Commit(); err != nil {
		return IngestResult{}, err
	}
	return res, nil
}

// IngestBatch runs a sequence of envelopes in ONE transaction: a bad envelope
// rejects the whole batch atomically.
func (s *Store) IngestBatch(ctx context.Context, envelopes []domain.Envelope) ([]IngestResult, error) {
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
	if err != nil {
		return nil, err
	}
	defer func() { _ = tx.Rollback() }()

	results := make([]IngestResult, 0, len(envelopes))
	for i := range envelopes {
		seq := envelopes[i].Sequence
		res, err := ingestInTx(ctx, tx, &envelopes[i].Block, &seq)
		if err != nil {
			return nil, fmt.Errorf("envelope %d (sequence %d, block %s): %w",
				i, envelopes[i].Sequence, envelopes[i].Block.Hash, err)
		}
		results = append(results, res)
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return results, nil
}

// advisory lock id serializing all writer transactions.
const writerLockID int64 = 0x464F524B // "FORK"

func ingestInTx(ctx context.Context, tx *sql.Tx, b *domain.Block, seq *int64) (IngestResult, error) {
	// One writer at a time: head/balance reasoning assumes a stable view of
	// the chain while the cascade connects. Readers take a snapshot and are
	// never blocked from seeing the last committed state.
	if _, err := tx.ExecContext(ctx, "SELECT pg_advisory_xact_lock($1)", writerLockID); err != nil {
		return IngestResult{}, err
	}

	var headBefore sql.NullString
	var cursor int64
	if err := tx.QueryRowContext(ctx,
		"SELECT head, cursor_seq FROM meta WHERE id = 1").Scan(&headBefore, &cursor); err != nil {
		return IngestResult{}, err
	}

	// --- delivery sequence handling (at-least-once, gap detecting) ---
	if seq != nil {
		if *seq <= cursor {
			var knownHash string
			err := tx.QueryRowContext(ctx,
				"SELECT block_hash FROM deliveries WHERE sequence = $1", *seq).Scan(&knownHash)
			switch {
			case err == nil && knownHash == b.Hash:
				// Idempotent replay of an already-committed delivery.
				return IngestResult{
					Hash:        b.Hash,
					Known:       true,
					BlockStatus: mustBlockStatus(ctx, tx, b.Hash),
					HeadBefore:  headBefore.String,
					HeadAfter:   headBefore.String,
					Cursor:      cursor,
				}, nil
			case err == nil:
				return IngestResult{}, fmt.Errorf("%w: sequence %d delivered block %s, retry has %s",
					domain.ErrSequenceTooSmall, *seq, knownHash, b.Hash)
			case errors.Is(err, sql.ErrNoRows):
				return IngestResult{}, fmt.Errorf("%w: sequence %d <= cursor %d but no delivery row",
					domain.ErrSequenceTooSmall, *seq, cursor)
			default:
				return IngestResult{}, err
			}
		}
		if *seq != cursor+1 {
			return IngestResult{}, fmt.Errorf("%w: expected %d, got %d",
				domain.ErrSequenceGap, cursor+1, *seq)
		}
	}

	// --- block identity: same hash must mean same content ---
	var (
		existingStatus string
		existingRaw    []byte
		known          bool
	)
	err := tx.QueryRowContext(ctx,
		"SELECT status, raw FROM blocks WHERE hash = $1", b.Hash).Scan(&existingStatus, &existingRaw)
	switch {
	case err == nil:
		known = true
		if string(existingRaw) != string(b.Raw) {
			return IngestResult{}, fmt.Errorf("%w: %s", domain.ErrSameHashDifferentContent, b.Hash)
		}
	case errors.Is(err, sql.ErrNoRows):
		// New block: insert staged first; connection happens below.
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO blocks (hash, parent_hash, height, status, in_chain, raw, first_seq)
			 VALUES ($1,$2,$3,'staged',FALSE,$4,$5)`,
			b.Hash, b.ParentHash, b.Height, b.Raw, seq); err != nil {
			return IngestResult{}, err
		}
		for i, t := range b.Transfers {
			if _, err := tx.ExecContext(ctx,
				`INSERT INTO transfers (block_hash, idx, from_addr, to_addr, amount)
				 VALUES ($1,$2,$3,$4,$5)`,
				b.Hash, i, t.From, t.To, t.Amount); err != nil {
				return IngestResult{}, err
			}
		}
	default:
		return IngestResult{}, err
	}

	// If the parent was rejected, this block can never connect: reject it and
	// everything staged beneath it.
	if !b.IsGenesis() {
		var parentStatus string
		err := tx.QueryRowContext(ctx, "SELECT status FROM blocks WHERE hash = $1", b.ParentHash).
			Scan(&parentStatus)
		if err == nil && parentStatus == "rejected" {
			if err := rejectCascade(ctx, tx, b.Hash); err != nil {
				return IngestResult{}, err
			}
			return finishDelivery(ctx, tx, b.Hash, known, "rejected", headBefore, seq, cursor)
		}
	}

	// --- connect the block and any staged descendants, one level at a time ---
	if err := connectCascade(ctx, tx); err != nil {
		return IngestResult{}, err
	}

	// --- choose the canonical tip, reorganize once against the old head ---
	headAfter, reorged, err := selectHeadAndReorg(ctx, tx, headBefore.String)
	if err != nil {
		return IngestResult{}, err
	}

	finalStatus, err := blockStatus(ctx, tx, b.Hash)
	if err != nil {
		return IngestResult{}, err
	}
	return finishDeliveryWithHead(ctx, tx, b.Hash, known, finalStatus,
		headBefore.String, headAfter, reorged, seq, cursor)
}

func mustBlockStatus(ctx context.Context, tx *sql.Tx, hash string) string {
	st, err := blockStatus(ctx, tx, hash)
	if err != nil {
		return ""
	}
	return st
}

func blockStatus(ctx context.Context, tx *sql.Tx, hash string) (string, error) {
	var st string
	if err := tx.QueryRowContext(ctx, "SELECT status FROM blocks WHERE hash = $1", hash).
		Scan(&st); err != nil {
		return "", err
	}
	return st, nil
}

// finishDelivery records the delivery row, advances the cursor and reads the
// resulting head. Used when no head change is possible (rejected cascade).
func finishDelivery(ctx context.Context, tx *sql.Tx, hash string, known bool, status string,
	headBefore sql.NullString, seq *int64, oldCursor int64) (IngestResult, error) {
	return finishDeliveryWithHead(ctx, tx, hash, known, status,
		headBefore.String, headBefore.String, false, seq, oldCursor)
}

func finishDeliveryWithHead(ctx context.Context, tx *sql.Tx, hash string, known bool, status,
	headBefore, headAfter string, reorged bool, seq *int64, oldCursor int64) (IngestResult, error) {
	if seq != nil {
		if _, err := tx.ExecContext(ctx,
			"INSERT INTO deliveries (sequence, block_hash) VALUES ($1,$2)", *seq, hash); err != nil {
			return IngestResult{}, err
		}
		if _, err := tx.ExecContext(ctx,
			"UPDATE meta SET cursor_seq = $1 WHERE id = 1", *seq); err != nil {
			return IngestResult{}, err
		}
		oldCursor = *seq
	}
	return IngestResult{
		Hash:        hash,
		Known:       known,
		BlockStatus: status,
		HeadBefore:  headBefore,
		HeadAfter:   headAfter,
		Reorg:       reorged && headBefore != headAfter,
		Cursor:      oldCursor,
	}, nil
}

// rejectCascade marks the root and all its staged descendants rejected.
func rejectCascade(ctx context.Context, tx *sql.Tx, root string) error {
	_, err := tx.ExecContext(ctx, `
WITH RECURSIVE bad(hash) AS (
    SELECT hash FROM blocks WHERE hash = $1
    UNION ALL
    SELECT child.hash FROM blocks child
    JOIN bad ON child.parent_hash = bad.hash
    WHERE child.status = 'staged'
)
UPDATE blocks SET status = 'rejected' WHERE hash IN (SELECT hash FROM bad)`, root)
	return err
}

type stagedConnect struct {
	hash       string
	height     int64
	parentHash string
	pHeight    int64
	pWeight    int64
	pInChain   bool
}

// connectCascade repeatedly promotes staged blocks whose parent is connected
// (and the unique genesis, if present). Blocks failing height/genesis rules
// are rejected together with their staged descendants.
func connectCascade(ctx context.Context, tx *sql.Tx) error {
	for {
		// Genesis candidate: parent zero, height 0, no connected genesis yet.
		var gHash string
		genesisExists := false
		err := tx.QueryRowContext(ctx, `SELECT COALESCE(
			(SELECT hash FROM blocks WHERE status='connected' AND height = 0 LIMIT 1), '')`).
			Scan(&gHash)
		if err != nil {
			return err
		}
		if gHash != "" {
			genesisExists = true
		}

		if !genesisExists {
			// Reject every zero-parent block whose height is not 0, including
			// all staged descendants: they can never become the genesis.
			res, err := tx.ExecContext(ctx, `
WITH RECURSIVE bad_roots AS (
    SELECT hash FROM blocks WHERE status='staged' AND parent_hash = $1 AND height <> 0
), bad(hash) AS (
    SELECT hash FROM bad_roots
    UNION ALL
    SELECT child.hash FROM blocks child
    JOIN bad ON child.parent_hash = bad.hash
    WHERE child.status = 'staged'
)
UPDATE blocks SET status='rejected' WHERE hash IN (SELECT hash FROM bad)`,
				domain.ZeroHash)
			if err != nil {
				return err
			}
			if n, err := res.RowsAffected(); err == nil && n > 0 {
				continue // re-evaluate candidates now that invalid roots are gone
			}

			var cand struct {
				hash   string
				height int64
				parent string
			}
			err = tx.QueryRowContext(ctx, `
SELECT hash, height, parent_hash FROM blocks
WHERE status = 'staged' AND parent_hash = $1 AND height = 0
ORDER BY hash ASC LIMIT 1`, domain.ZeroHash).
				Scan(&cand.hash, &cand.height, &cand.parent)
			switch {
			case err == nil:
				// Genesis carries weight 1 (each block contributes weight 1),
				// so a chain's cumulative weight equals its block count.
				if err := connectBlock(ctx, tx, cand.hash, true, 1, false); err != nil {
					return err
				}
				continue
			case errors.Is(err, sql.ErrNoRows):
				// fall through to parent-connected scan
			default:
				return err
			}
		} else {
			// A genesis already exists: any further zero-parent block is an
			// illegal competing genesis (regardless of height).
			res, err := tx.ExecContext(ctx, `
WITH RECURSIVE bad_roots AS (
    SELECT hash FROM blocks WHERE status='staged' AND parent_hash = $1
), bad(hash) AS (
    SELECT hash FROM bad_roots
    UNION ALL
    SELECT child.hash FROM blocks child
    JOIN bad ON child.parent_hash = bad.hash
    WHERE child.status = 'staged'
)
UPDATE blocks SET status='rejected' WHERE hash IN (SELECT hash FROM bad)`,
				domain.ZeroHash)
			if err != nil {
				return err
			}
			if n, err := res.RowsAffected(); err == nil && n > 0 {
				continue
			}
		}

		var c stagedConnect
		err = tx.QueryRowContext(ctx, `
SELECT s.hash, s.height, s.parent_hash, p.height, p.cum_weight, p.in_chain
FROM blocks s JOIN blocks p ON p.hash = s.parent_hash
WHERE s.status = 'staged' AND p.status = 'connected'
ORDER BY s.hash ASC LIMIT 1`).Scan(&c.hash, &c.height, &c.parentHash, &c.pHeight, &c.pWeight, &c.pInChain)
		switch {
		case err == nil:
			// Structural rules fixed by the agreed model.
			if c.parentHash == domain.ZeroHash {
				// Non-genesis block claiming zero parent.
				if err := rejectCascade(ctx, tx, c.hash); err != nil {
					return err
				}
				continue
			}
			if c.height != c.pHeight+1 {
				if err := rejectCascade(ctx, tx, c.hash); err != nil {
					return err
				}
				continue
			}
			if err := connectBlock(ctx, tx, c.hash, false, c.pWeight+1, c.pInChain); err != nil {
				return err
			}
		case errors.Is(err, sql.ErrNoRows):
			return nil
		default:
			return err
		}
	}
}

// connectBlock promotes one staged block to connected. in_chain is always
// false here: selectHeadAndReorg recomputes chain membership authoritatively
// after the whole cascade has connected, so side forks can never inherit the
// fork point's membership.
func connectBlock(ctx context.Context, tx *sql.Tx, hash string, genesis bool,
	weight int64, _ bool) error {
	_, err := tx.ExecContext(ctx,
		"UPDATE blocks SET status='connected', cum_weight=$2, in_chain=FALSE WHERE hash=$1",
		hash, weight)
	return err
}

// selectHeadAndReorg picks the heaviest connected tip (tie: smallest hash) and
// makes balances/in_chain describe exactly that chain. One reorg per ingest,
// inside the caller's transaction.
func selectHeadAndReorg(ctx context.Context, tx *sql.Tx, oldHead string) (string, bool, error) {
	var newHead sql.NullString
	err := tx.QueryRowContext(ctx, `
SELECT hash FROM blocks
WHERE status = 'connected'
ORDER BY cum_weight DESC, hash ASC
LIMIT 1`).Scan(&newHead)
	if errors.Is(err, sql.ErrNoRows) || newHead.String == "" {
		return "", false, nil
	}
	if err != nil {
		return "", false, err
	}
	if newHead.String == oldHead {
		// Old head still best: make sure its full chain is marked. This also
		// covers "block attached onto old head" where the new connected block
		// started with in_chain = false.
		oldChain, err := ancestry(ctx, tx, oldHead)
		if err != nil {
			return "", false, err
		}
		// Side forks must stay unmarked.
		if _, err := tx.ExecContext(ctx,
			"UPDATE blocks SET in_chain = FALSE WHERE status = 'connected'"); err != nil {
			return "", false, err
		}
		if err := flipInChain(ctx, tx, oldChain, true); err != nil {
			return "", false, err
		}
		return oldHead, false, nil
	}

	// oldHead == "" means first-ever connection: apply the whole chain.
	oldChain, err := ancestry(ctx, tx, oldHead)
	if err != nil {
		return "", false, err
	}
	newChain, err := ancestry(ctx, tx, newHead.String)
	if err != nil {
		return "", false, err
	}

	// Split at the common ancestor (hash sets; chains merge at one point).
	oldSet := make(map[string]bool, len(oldChain))
	for _, h := range oldChain {
		oldSet[h] = true
	}
	split := len(newChain)
	for i, h := range newChain {
		if oldSet[h] {
			split = i
			break
		}
	}
	var undo, apply []string
	if len(oldChain) > 0 {
		// oldChain order is tip->genesis; undo until ancestor excluded.
		for _, h := range oldChain {
			if h == newChain[split] {
				break
			}
			undo = append(undo, h)
		}
	}
	// apply newChain[0:split] (tip side, exclusive of ancestor)
	apply = append(apply, newChain[:split]...)

	// Recompute membership from scratch: every connected block unmarked, then
	// exactly the winning chain marked. Balances are reconciled by applying
	// undo (old side) and apply (new side); the shared prefix is untouched.
	if _, err := tx.ExecContext(ctx,
		"UPDATE blocks SET in_chain = FALSE WHERE status = 'connected'"); err != nil {
		return "", false, err
	}
	if err := flipInChain(ctx, tx, newChain, true); err != nil {
		return "", false, err
	}
	if err := applyBalanceDeltas(ctx, tx, undo, -1); err != nil {
		return "", false, err
	}
	if err := applyBalanceDeltas(ctx, tx, apply, +1); err != nil {
		return "", false, err
	}
	if _, err := tx.ExecContext(ctx, "UPDATE meta SET head = $1 WHERE id = 1", newHead.String); err != nil {
		return "", false, err
	}
	return newHead.String, true, nil
}

// ancestry returns the chain hash..genesis ordered tip-first. Empty hash
// yields an empty slice.
func ancestry(ctx context.Context, tx *sql.Tx, tip string) ([]string, error) {
	if tip == "" {
		return nil, nil
	}
	rows, err := tx.QueryContext(ctx, `
WITH RECURSIVE chain(hash, parent_hash) AS (
    SELECT hash, parent_hash FROM blocks WHERE hash = $1
    UNION ALL
    SELECT b.hash, b.parent_hash FROM blocks b
    JOIN chain c ON c.parent_hash = b.hash
)
SELECT hash FROM chain`, tip)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var h string
		if err := rows.Scan(&h); err != nil {
			return nil, err
		}
		out = append(out, h)
	}
	return out, rows.Err()
}

func flipInChain(ctx context.Context, tx *sql.Tx, hashes []string, in bool) error {
	if len(hashes) == 0 {
		return nil
	}
	_, err := tx.ExecContext(ctx,
		"UPDATE blocks SET in_chain = $2 WHERE hash = ANY($1)", hashes, in)
	return err
}

// applyBalanceDeltas aggregates the transfers of the given blocks in Go
// (with checked int64 arithmetic) and writes one upsert per touched account.
// sign -1 removes a chain's effects (reorg undo), +1 adds them.
func applyBalanceDeltas(ctx context.Context, tx *sql.Tx, hashes []string, sign int64) error {
	if len(hashes) == 0 {
		return nil
	}
	delta := map[string]int64{}
	rows, err := tx.QueryContext(ctx, `
SELECT block_hash, idx, from_addr, to_addr, amount
FROM transfers WHERE block_hash = ANY($1)
ORDER BY block_hash, idx`, hashes)
	if err != nil {
		return err
	}
	type trow struct {
		from, to string
		amount   int64
	}
	var collected []trow
	for rows.Next() {
		var bh, from, to string
		var idx int
		var amount int64
		if err := rows.Scan(&bh, &idx, &from, &to, &amount); err != nil {
			rows.Close()
			return err
		}
		collected = append(collected, trow{from, to, amount})
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return err
	}
	// Aggregate net delta; self-transfers cancel.
	for _, t := range collected {
		if t.from == t.to {
			continue
		}
		d := t.amount * sign
		if err := addDelta(delta, t.from, -d); err != nil {
			return err
		}
		if err := addDelta(delta, t.to, d); err != nil {
			return err
		}
	}

	// Read-modify-write current balances within the same tx (advisory lock
	// already held, and the row locks taken here serialize against nothing
	// else because writers are globally serialized).
	for addr, d := range delta {
		if d == 0 {
			continue
		}
		var current int64
		err := tx.QueryRowContext(ctx, "SELECT amount FROM balances WHERE address = $1", addr).Scan(&current)
		switch {
		case errors.Is(err, sql.ErrNoRows):
			current = 0
		case err != nil:
			return err
		}
		next, err := checkedAdd(current, d)
		if err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `
INSERT INTO balances (address, amount) VALUES ($1,$2)
ON CONFLICT (address) DO UPDATE SET amount = EXCLUDED.amount`, addr, next); err != nil {
			return err
		}
	}
	return nil
}

func addDelta(m map[string]int64, addr string, d int64) error {
	v := m[addr]
	nv, err := checkedAdd(v, d)
	if err != nil {
		return err
	}
	m[addr] = nv
	return nil
}

func checkedAdd(a, b int64) (int64, error) {
	if b > 0 && a > (1<<63-1)-b {
		return 0, domain.ErrBalanceOverflow
	}
	if b < 0 && a < (-1<<63)-b {
		return 0, domain.ErrBalanceOverflow
	}
	return a + b, nil
}
