// Package store persists the verified chain and provenance evidence in SQLite.
//
// The durability rule is encoded here: only the contiguous prefix that passed
// cryptographic verification is ever written, and it is written together with
// an advanced checkpoint in one transaction. Out-of-order segments that arrive
// before the gap they depend on are held in memory by the syncer, never in the
// database, so a restart resumes strictly from the last committed checkpoint
// and never trusts an unverified remote anchor.
package store

import (
	"context"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"
	"sync"

	_ "modernc.org/sqlite"

	"nodesync/internal/chain"
)

// Evidence records the provenance of a single fetch attempt or decision.
type Evidence struct {
	ID         int64
	AtUnixMs   int64
	NodeID     string
	Start      int64
	End        int64
	Attempt    int
	Outcome    string // "ok" | "rpc_error" | "timeout" | "hash_mismatch" | "parent_mismatch" | "shape" | "advertised_mismatch" | "stale_ignored" | "published"
	Detail     string
	RemoteHeig int64 // remote advertised height observed, else -1
}

// Store wraps the SQLite database.
type Store struct {
	mu sync.Mutex
	db *sql.DB
}

// Open opens (creating if needed) the database at path. Use ":memory:" for
// ephemeral databases.
func Open(ctx context.Context, path string) (*Store, error) {
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, fmt.Errorf("store: open: %w", err)
	}
	// modernc.org/sqlite is safe with one writer; keep pool small and serial.
	db.SetMaxOpenConns(1)
	if _, err := db.ExecContext(ctx, schemaSQL); err != nil {
		db.Close()
		return nil, fmt.Errorf("store: schema: %w", err)
	}
	return &Store{db: db}, nil
}

// Close releases the database.
func (s *Store) Close() error { return s.db.Close() }

const schemaSQL = `
CREATE TABLE IF NOT EXISTS metadata (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS blocks (
  height      INTEGER PRIMARY KEY,
  parent_hash BLOB NOT NULL,
  hash        BLOB NOT NULL,
  timestamp   INTEGER NOT NULL,
  body        BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  at_unix_ms   INTEGER NOT NULL,
  node_id      TEXT NOT NULL,
  start_height INTEGER NOT NULL,
  end_height   INTEGER NOT NULL,
  attempt      INTEGER NOT NULL,
  outcome      TEXT NOT NULL,
  detail       TEXT NOT NULL,
  remote_height INTEGER NOT NULL
);
`

// InitializeGenesis seeds an empty database with the trusted genesis block and
// checkpoint 0. It errors if the database already contains blocks.
func (s *Store) InitializeGenesis(ctx context.Context, g chain.Block) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var n int
	if err := tx.QueryRowContext(ctx, `SELECT COUNT(*) FROM blocks`).Scan(&n); err != nil {
		return err
	}
	if n > 0 {
		return errors.New("store: database already initialized")
	}
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO blocks(height,parent_hash,hash,timestamp,body) VALUES(?,?,?,?,?)`,
		g.Height, g.ParentHash, g.Hash, g.Timestamp, g.Body); err != nil {
		return err
	}
	if err := setMetaTx(ctx, tx, "checkpoint_height", fmt.Sprintf("%d", g.Height)); err != nil {
		return err
	}
	if err := setMetaTx(ctx, tx, "checkpoint_hash", hex.EncodeToString(g.Hash)); err != nil {
		return err
	}
	return tx.Commit()
}

// Initialized reports whether genesis was seeded.
func (s *Store) Initialized(ctx context.Context) (bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var n int
	if err := s.db.QueryRowContext(ctx, `SELECT COUNT(*) FROM blocks`).Scan(&n); err != nil {
		return false, err
	}
	return n > 0, nil
}

// Checkpoint returns the last verified-and-published height and its hash.
func (s *Store) Checkpoint(ctx context.Context) (int64, []byte, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.checkpointLocked(ctx)
}

func (s *Store) checkpointLocked(ctx context.Context) (int64, []byte, error) {
	var hStr, hashStr string
	row := s.db.QueryRowContext(ctx, `SELECT value FROM metadata WHERE key='checkpoint_height'`)
	if err := row.Scan(&hStr); err != nil {
		return 0, nil, fmt.Errorf("store: checkpoint_height: %w", err)
	}
	row = s.db.QueryRowContext(ctx, `SELECT value FROM metadata WHERE key='checkpoint_hash'`)
	if err := row.Scan(&hashStr); err != nil {
		return 0, nil, fmt.Errorf("store: checkpoint_hash: %w", err)
	}
	var h int64
	if _, err := fmt.Sscanf(hStr, "%d", &h); err != nil {
		return 0, nil, fmt.Errorf("store: bad checkpoint height %q", hStr)
	}
	hash, err := hex.DecodeString(hashStr)
	if err != nil {
		return 0, nil, fmt.Errorf("store: checkpoint hash: %w", err)
	}
	return h, hash, nil
}

// AppendVerified atomically stores a contiguous verified segment (whose first
// block links to parentHash at expectedHeight) and advances the checkpoint.
func (s *Store) AppendVerified(ctx context.Context, blocks []chain.Block) error {
	if len(blocks) == 0 {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	cp, cpHash, err := s.checkpointLocked(ctx)
	if err != nil {
		return err
	}
	if err := chain.VerifyAppend(cpHash, cp+1, blocks); err != nil {
		return fmt.Errorf("store: refusing non-contiguous append: %w", err)
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	stmt, err := tx.PrepareContext(ctx,
		`INSERT OR REPLACE INTO blocks(height,parent_hash,hash,timestamp,body) VALUES(?,?,?,?,?)`)
	if err != nil {
		return err
	}
	defer stmt.Close()
	for i := range blocks {
		b := &blocks[i]
		if _, err := stmt.ExecContext(ctx, b.Height, b.ParentHash, b.Hash, b.Timestamp, b.Body); err != nil {
			return fmt.Errorf("store: insert height %d: %w", b.Height, err)
		}
	}
	last := blocks[len(blocks)-1]
	if err := setMetaTx(ctx, tx, "checkpoint_height", fmt.Sprintf("%d", last.Height)); err != nil {
		return err
	}
	if err := setMetaTx(ctx, tx, "checkpoint_hash", hex.EncodeToString(last.Hash)); err != nil {
		return err
	}
	return tx.Commit()
}

// Block returns the stored block at a height.
func (s *Store) Block(ctx context.Context, height int64) (chain.Block, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.blockLocked(ctx, height)
}

func (s *Store) blockLocked(ctx context.Context, height int64) (chain.Block, error) {
	var b chain.Block
	row := s.db.QueryRowContext(ctx,
		`SELECT height,parent_hash,hash,timestamp,body FROM blocks WHERE height=?`, height)
	if err := row.Scan(&b.Height, &b.ParentHash, &b.Hash, &b.Timestamp, &b.Body); err != nil {
		return chain.Block{}, fmt.Errorf("store: block %d: %w", height, err)
	}
	return b, nil
}

// Chain returns all stored blocks in height order (the published prefix).
func (s *Store) Chain(ctx context.Context) ([]chain.Block, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	rows, err := s.db.QueryContext(ctx,
		`SELECT height,parent_hash,hash,timestamp,body FROM blocks ORDER BY height`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []chain.Block
	for rows.Next() {
		var b chain.Block
		if err := rows.Scan(&b.Height, &b.ParentHash, &b.Hash, &b.Timestamp, &b.Body); err != nil {
			return nil, err
		}
		out = append(out, b)
	}
	return out, rows.Err()
}

// AddEvidence records a provenance row.
func (s *Store) AddEvidence(ctx context.Context, e Evidence) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.ExecContext(ctx,
		`INSERT INTO evidence(at_unix_ms,node_id,start_height,end_height,attempt,outcome,detail,remote_height)
		 VALUES(?,?,?,?,?,?,?,?)`,
		e.AtUnixMs, e.NodeID, e.Start, e.End, e.Attempt, e.Outcome, e.Detail, e.RemoteHeig)
	return err
}

// Evidence returns all evidence rows in insertion order.
func (s *Store) Evidence(ctx context.Context) ([]Evidence, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	rows, err := s.db.QueryContext(ctx,
		`SELECT id,at_unix_ms,node_id,start_height,end_height,attempt,outcome,detail,remote_height
		 FROM evidence ORDER BY id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Evidence
	for rows.Next() {
		var e Evidence
		if err := rows.Scan(&e.ID, &e.AtUnixMs, &e.NodeID, &e.Start, &e.End,
			&e.Attempt, &e.Outcome, &e.Detail, &e.RemoteHeig); err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, rows.Err()
}

func setMetaTx(ctx context.Context, tx *sql.Tx, key, value string) error {
	_, err := tx.ExecContext(ctx,
		`INSERT INTO metadata(key,value) VALUES(?,?)
		 ON CONFLICT(key) DO UPDATE SET value=excluded.value`, key, value)
	return err
}
