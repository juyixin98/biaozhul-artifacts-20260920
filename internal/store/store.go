// Package store contains all PostgreSQL access for the inbox. SQL lives here;
// the inbox package composes these operations inside transactions.
package store

import (
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

//go:embed migrations/0001_init.sql
var migrationSQL string

// DBTX is the surface shared by a pooled connection and a transaction.
type DBTX interface {
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// Queries runs SQL against a pool or a transaction.
type Queries struct {
	DB DBTX
}

// Store is the root data-access handle.
type Store struct {
	Pool *pgxpool.Pool
	*Queries
}

// New wraps a pool.
func New(pool *pgxpool.Pool) *Store {
	return &Store{Pool: pool, Queries: &Queries{DB: pool}}
}

// WithTx runs fn inside a single transaction.
func (s *Store) WithTx(ctx context.Context, fn func(q *Queries) error) error {
	tx, err := s.Pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx) //nolint:errcheck // no-op after commit
	if err := fn(&Queries{DB: tx}); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// Migrate applies the embedded schema (idempotent IF NOT EXISTS DDL).
func (s *Store) Migrate(ctx context.Context) error {
	if _, err := s.Pool.Exec(ctx, migrationSQL); err != nil {
		return fmt.Errorf("apply migration: %w", err)
	}
	return nil
}

// ErrNotFound is returned by single-row lookups.
var ErrNotFound = errors.New("not found")

// Sentinel block conflicts returned by the reorg-aware block proposal.
var (
	ErrFinalConflict   = errors.New("a final block already occupies that height with a different hash")
	ErrBadParent       = errors.New("parent block is missing or revoked")
	ErrBadParentHeight = errors.New("parent block height must be height-1")
)

// Model types -------------------------------------------------------------

type Block struct {
	SourceChain string `json:"source_chain"`
	Height      int64  `json:"height"`
	Hash        string `json:"hash"`
	ParentHash  string `json:"parent_hash"`
	Status      string `json:"status"`
}

type Channel struct {
	SourceChain  string `json:"source_chain"`
	ChannelID    string `json:"channel_id"`
	Status       string `json:"status"`
	FrozenReason string `json:"frozen_reason,omitempty"`
}

type Message struct {
	SourceChain   string          `json:"source_chain"`
	ChannelID     string          `json:"channel_id"`
	Sequence      int64           `json:"sequence"`
	BlockHash     string          `json:"block_hash"`
	BodyDigest    string          `json:"body_digest"`
	Body          json.RawMessage `json:"body"`
	Status        string          `json:"status"`
	FailureReason string          `json:"failure_reason,omitempty"`
}

type Evidence struct {
	ID             int64           `json:"id"`
	SourceChain    string          `json:"source_chain"`
	ChannelID      string          `json:"channel_id"`
	Sequence       int64           `json:"sequence"`
	BlockHash      string          `json:"block_hash"`
	BodyDigest     string          `json:"body_digest"`
	ExistingStatus string          `json:"existing_status"`
	Body           json.RawMessage `json:"body"`
}

type Alert struct {
	ID          int64  `json:"id"`
	SourceChain string `json:"source_chain"`
	ChannelID   string `json:"channel_id"`
	Kind        string `json:"kind"`
	Sequence    *int64 `json:"sequence,omitempty"`
	BodyDigest  string `json:"body_digest,omitempty"`
	Detail      string `json:"detail"`
	CreatedAt   string `json:"created_at"`
}

type Account struct {
	Address string `json:"address"`
	Balance int64  `json:"balance"`
}

// Chains -------------------------------------------------------------------

// EnsureChains inserts the simulated source chains if missing.
func (q *Queries) EnsureChains(ctx context.Context, names []string) error {
	for _, n := range names {
		if _, err := q.DB.Exec(ctx,
			`INSERT INTO chains(name) VALUES ($1) ON CONFLICT DO NOTHING`, n); err != nil {
			return err
		}
	}
	return nil
}

// Blocks -------------------------------------------------------------------

// BlockExistsAtHeight returns the live block (if any) at a chain height.
func (q *Queries) BlockExistsAtHeight(ctx context.Context, chain string, height int64) (*Block, bool, error) {
	row := q.DB.QueryRow(ctx,
		`SELECT source_chain, height, hash, parent_hash, status
		   FROM blocks WHERE source_chain=$1 AND height=$2 AND status <> 'revoked'`,
		chain, height)
	b, err := scanBlock(row)
	if errors.Is(err, ErrNotFound) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	return b, true, nil
}

// GetBlock fetches a block by hash regardless of status.
func (q *Queries) GetBlock(ctx context.Context, chain, hash string) (*Block, error) {
	return scanBlock(q.DB.QueryRow(ctx,
		`SELECT source_chain, height, hash, parent_hash, status
		   FROM blocks WHERE source_chain=$1 AND hash=$2`, chain, hash))
}

// InsertProposedBlock stores a new proposed block.
func (q *Queries) InsertProposedBlock(ctx context.Context, chain, hash, parentHash string, height int64) error {
	_, err := q.DB.Exec(ctx,
		`INSERT INTO blocks(source_chain, height, hash, parent_hash, status)
		 VALUES ($1,$2,$3,$4,'proposed')`, chain, height, hash, parentHash)
	return err
}

// RevokeBlocksFrom revokes every live block at height >= fromHeight and marks
// pending messages bound to those blocks cancelled. It must run in the same
// transaction as the replacement block insert. Returns how many blocks were
// revoked.
func (q *Queries) RevokeBlocksFrom(ctx context.Context, chain string, fromHeight int64) (int64, error) {
	tag, err := q.DB.Exec(ctx,
		`UPDATE blocks SET status='revoked'
		   WHERE source_chain=$1 AND height>=$2 AND status <> 'revoked'`,
		chain, fromHeight)
	if err != nil {
		return 0, err
	}
	n := tag.RowsAffected()
	if n > 0 {
		if _, err := q.DB.Exec(ctx,
			`UPDATE messages SET status='cancelled', updated_at=now()
			   WHERE source_chain=$1 AND block_hash IN (
			       SELECT hash FROM blocks
			         WHERE source_chain=$1 AND height>=$2 AND status='revoked'
			   ) AND status='pending'`, chain, fromHeight); err != nil {
			return 0, err
		}
	}
	return n, nil
}

// FinalizeBlock marks one block final. Exact-once confirmed semantics:
// a final block is never revoked afterwards.
func (q *Queries) FinalizeBlock(ctx context.Context, chain, hash string) (bool, error) {
	tag, err := q.DB.Exec(ctx,
		`UPDATE blocks SET status='final', finalized_at=now()
		  WHERE source_chain=$1 AND hash=$2 AND status='proposed'`, chain, hash)
	if err != nil {
		return false, err
	}
	return tag.RowsAffected() == 1, nil
}

// FinalizeAncestors finalizes every proposed ancestor of the given block hash
// following parent_hash links (cascading confirmation).
//
// It walks the chain from the target block toward genesis, collecting live
// proposed blocks, then flips all of them (including the target) to final in
// height order. A broken/revoked link leaves an error.
func (q *Queries) FinalizeAncestors(ctx context.Context, chain, hash string) error {
	type link struct {
		height int64
		hash   string
	}
	var links []link
	cur := hash
	seen := map[string]bool{}
	for {
		var (
			h      int64
			bh, ph string
			status string
		)
		err := q.DB.QueryRow(ctx,
			`SELECT height, hash, parent_hash, status FROM blocks
			  WHERE source_chain=$1 AND hash=$2`, chain, cur).
			Scan(&h, &bh, &ph, &status)
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return err
		}
		if status == "revoked" {
			return fmt.Errorf("block %s at height %d is revoked, cannot finalize", bh, h)
		}
		if seen[bh] {
			return fmt.Errorf("cycle in parent chain at %s", bh)
		}
		seen[bh] = true
		if status == "proposed" {
			links = append(links, link{height: h, hash: bh})
		}
		if h == 1 || ph == "0x0" || ph == "" {
			break
		}
		cur = ph
	}
	// Lowest height first so confirmation advances in chain order.
	for i, j := 0, len(links)-1; i < j; i, j = i+1, j-1 {
		links[i], links[j] = links[j], links[i]
	}
	for _, l := range links {
		if _, err := q.DB.Exec(ctx,
			`UPDATE blocks SET status='final', finalized_at=now()
			  WHERE source_chain=$1 AND hash=$2 AND status='proposed'`,
			chain, l.hash); err != nil {
			return err
		}
	}
	return nil
}

// HasPendingOnBlock reports whether any pending message is anchored to a block.
func (q *Queries) HasPendingOnBlock(ctx context.Context, chain, hash string) (bool, error) {
	var exists bool
	err := q.DB.QueryRow(ctx,
		`SELECT EXISTS(SELECT 1 FROM messages
		                WHERE source_chain=$1 AND block_hash=$2 AND status='pending')`,
		chain, hash).Scan(&exists)
	return exists, err
}

// ListLiveBlocksAfter lists live blocks with height > afterHeight (reorg helpers/tests).
func (q *Queries) ListLiveBlocksAfter(ctx context.Context, chain string, afterHeight int64) ([]Block, error) {
	rows, err := q.DB.Query(ctx,
		`SELECT source_chain, height, hash, parent_hash, status
		   FROM blocks WHERE source_chain=$1 AND height>$2 AND status<>'revoked'
		  ORDER BY height`, chain, afterHeight)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Block
	for rows.Next() {
		b, err := scanBlock(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, *b)
	}
	return out, rows.Err()
}

// Channels -----------------------------------------------------------------

// EnsureChannel creates a channel row if absent.
func (q *Queries) EnsureChannel(ctx context.Context, chain, channel string) error {
	_, err := q.DB.Exec(ctx,
		`INSERT INTO channels(source_chain, channel_id) VALUES ($1,$2)
		 ON CONFLICT DO NOTHING`, chain, channel)
	return err
}

// LockChannel takes a row lock on the channel and returns it.
func (q *Queries) LockChannel(ctx context.Context, chain, channel string) (*Channel, error) {
	row := q.DB.QueryRow(ctx,
		`SELECT source_chain, channel_id, status, COALESCE(frozen_reason,'')
		   FROM channels WHERE source_chain=$1 AND channel_id=$2
		   FOR UPDATE`, chain, channel)
	var c Channel
	err := row.Scan(&c.SourceChain, &c.ChannelID, &c.Status, &c.FrozenReason)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	return &c, nil
}

// FreezeChannel freezes a channel and records the reason. Freeze is sticky.
func (q *Queries) FreezeChannel(ctx context.Context, chain, channel, reason string) error {
	_, err := q.DB.Exec(ctx,
		`UPDATE channels SET status='frozen', frozen_reason=$3, frozen_at=now()
		  WHERE source_chain=$1 AND channel_id=$2 AND status='active'`,
		chain, channel, reason)
	return err
}

// ListActiveChannels lists channel keys (used by startup recovery sweep).
func (q *Queries) ListActiveChannels(ctx context.Context) ([]Channel, error) {
	rows, err := q.DB.Query(ctx,
		`SELECT source_chain, channel_id, status, COALESCE(frozen_reason,'')
		   FROM channels WHERE status='active' ORDER BY source_chain, channel_id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Channel
	for rows.Next() {
		var c Channel
		if err := rows.Scan(&c.SourceChain, &c.ChannelID, &c.Status, &c.FrozenReason); err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, rows.Err()
}

// GetChannel reads a channel without locking.
func (q *Queries) GetChannel(ctx context.Context, chain, channel string) (*Channel, error) {
	row := q.DB.QueryRow(ctx,
		`SELECT source_chain, channel_id, status, COALESCE(frozen_reason,'')
		   FROM channels WHERE source_chain=$1 AND channel_id=$2`, chain, channel)
	var c Channel
	err := row.Scan(&c.SourceChain, &c.ChannelID, &c.Status, &c.FrozenReason)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	return &c, nil
}

// Messages -----------------------------------------------------------------

// GetMessage fetches the stored message for a key, if any.
func (q *Queries) GetMessage(ctx context.Context, chain, channel string, seq int64) (*Message, error) {
	row := q.DB.QueryRow(ctx,
		`SELECT source_chain, channel_id, sequence, block_hash, body_digest, body,
		        status, COALESCE(failure_reason,'')
		   FROM messages WHERE source_chain=$1 AND channel_id=$2 AND sequence=$3`,
		chain, channel, seq)
	return scanMessage(row)
}

// InsertPendingMessage stores a brand-new candidate message.
func (q *Queries) InsertPendingMessage(ctx context.Context, m *Message) error {
	_, err := q.DB.Exec(ctx,
		`INSERT INTO messages
		   (source_chain, channel_id, sequence, block_hash, body_digest, body, status)
		 VALUES ($1,$2,$3,$4,$5,$6::jsonb,'pending')`,
		m.SourceChain, m.ChannelID, m.Sequence, m.BlockHash, m.BodyDigest, string(m.Body))
	return err
}

// ReanchorMessage points an existing message at a replacement live block and
// returns it to pending (re-delivery after a reorg cancelled it, or a relayer
// re-submission under a new not-yet-revoked block).
func (q *Queries) ReanchorMessage(ctx context.Context, chain, channel string, seq int64, blockHash string) error {
	tag, err := q.DB.Exec(ctx,
		`UPDATE messages SET block_hash=$4, status='pending',
		                    failure_reason=NULL, updated_at=now()
		  WHERE source_chain=$1 AND channel_id=$2 AND sequence=$3
		    AND status IN ('pending','cancelled')`,
		chain, channel, seq, blockHash)
	if err != nil {
		return err
	}
	if tag.RowsAffected() != 1 {
		return fmt.Errorf("reanchor: unexpected rows affected %d", tag.RowsAffected())
	}
	return nil
}

// NextPendingMessage returns the earliest pending message at or after seq on
// a channel, along with its block status. Rows are locked so only one
// advancement process handles them.
func (q *Queries) NextPendingMessage(ctx context.Context, chain, channel string, seq int64) (*Message, string, error) {
	row := q.DB.QueryRow(ctx,
		`SELECT m.source_chain, m.channel_id, m.sequence, m.block_hash,
		        m.body_digest, m.body, m.status, COALESCE(m.failure_reason,''),
		        b.status
		   FROM messages m
		   JOIN blocks b
		     ON b.source_chain = m.source_chain AND b.hash = m.block_hash
		  WHERE m.source_chain=$1 AND m.channel_id=$2
		    AND m.sequence>=$3 AND m.status='pending'
		  ORDER BY m.sequence
		  LIMIT 1
		  FOR UPDATE OF m`,
		chain, channel, seq)
	return scanMessageWithBlock(row)
}

// LastHandledSequence returns the highest sequence that has EXECUTED
// successfully, or -1 when nothing has. A message in execute_failed does NOT
// advance this counter: the failing sequence blocks the prefix (later
// messages cannot skip it), which is how execution failures surface.
func (q *Queries) LastHandledSequence(ctx context.Context, chain, channel string) (int64, error) {
	var last *int64
	err := q.DB.QueryRow(ctx,
		`SELECT MAX(sequence) FROM messages
		  WHERE source_chain=$1 AND channel_id=$2 AND status='executed'`,
		chain, channel).Scan(&last)
	if err != nil {
		return 0, err
	}
	if last == nil {
		return -1, nil
	}
	return *last, nil
}

// MarkExecuted commits an execution inside the caller's transaction:
// message -> executed, executions ledger row, account balances applied.
func (q *Queries) MarkExecuted(ctx context.Context, chain, channel, digest string, seq int64) error {
	tag, err := q.DB.Exec(ctx,
		`UPDATE messages SET status='executed', executed_at=now(),
		                    failure_reason=NULL, updated_at=now()
		  WHERE source_chain=$1 AND channel_id=$2 AND sequence=$3 AND status='pending'`,
		chain, channel, seq)
	if err != nil {
		return err
	}
	if tag.RowsAffected() != 1 {
		return fmt.Errorf("mark executed: expected 1 row, got %d", tag.RowsAffected())
	}
	_, err = q.DB.Exec(ctx,
		`INSERT INTO executions(source_chain, channel_id, sequence, body_digest)
		 VALUES ($1,$2,$3,$4)
		 ON CONFLICT (source_chain, channel_id, sequence) DO NOTHING`,
		chain, channel, seq, digest)
	return err
}

// MarkExecutionFailed records a terminal application failure. The channel
// stays open but advancement stops (the next expected sequence remains this
// one; an operator must fix the payload before anything later can execute).
func (q *Queries) MarkExecutionFailed(ctx context.Context, chain, channel string, seq int64, reason string) error {
	_, err := q.DB.Exec(ctx,
		`UPDATE messages SET status='execute_failed', failure_reason=$4, updated_at=now()
		  WHERE source_chain=$1 AND channel_id=$2 AND sequence=$3 AND status='pending'`,
		chain, channel, seq, reason)
	return err
}

// ListMessages returns messages on a channel ordered by sequence.
func (q *Queries) ListMessages(ctx context.Context, chain, channel string, limit int) ([]Message, error) {
	rows, err := q.DB.Query(ctx,
		`SELECT source_chain, channel_id, sequence, block_hash, body_digest, body,
		        status, COALESCE(failure_reason,'')
		   FROM messages WHERE source_chain=$1 AND channel_id=$2
		  ORDER BY sequence LIMIT $3`,
		chain, channel, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return collectMessages(rows)
}

// Evidence & alerts --------------------------------------------------------

// InsertEvidence preserves a conflicting payload. The unique constraint on
// (key, body_digest) makes repeated identical conflicts idempotent; returns
// true when this is the first observation.
func (q *Queries) InsertEvidence(ctx context.Context, e *Evidence) (bool, error) {
	tag, err := q.DB.Exec(ctx,
		`INSERT INTO message_evidence
		   (source_chain, channel_id, sequence, block_hash, body_digest, body, existing_status)
		 VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7)
		 ON CONFLICT (source_chain, channel_id, sequence, body_digest) DO NOTHING`,
		e.SourceChain, e.ChannelID, e.Sequence, e.BlockHash, e.BodyDigest, string(e.Body), e.ExistingStatus)
	if err != nil {
		return false, err
	}
	return tag.RowsAffected() == 1, nil
}

// InsertAlert raises a channel alert.
func (q *Queries) InsertAlert(ctx context.Context, a *Alert) error {
	_, err := q.DB.Exec(ctx,
		`INSERT INTO alerts(source_chain, channel_id, kind, sequence, body_digest, detail)
		 VALUES ($1,$2,$3,$4,$5,$6)`,
		a.SourceChain, a.ChannelID, a.Kind, a.Sequence, nullable(a.BodyDigest), a.Detail)
	return err
}

// ListAlerts lists alerts, optionally scoped to one channel.
func (q *Queries) ListAlerts(ctx context.Context, chain, channel string, limit int) ([]Alert, error) {
	rows, err := q.DB.Query(ctx,
		`SELECT id, source_chain, channel_id, kind, sequence,
		        COALESCE(body_digest,''), detail, to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SS.USZ')
		   FROM alerts
		  WHERE ($1='' OR source_chain=$1)
		    AND ($2='' OR channel_id=$2)
		  ORDER BY id DESC LIMIT $3`,
		chain, channel, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Alert
	for rows.Next() {
		var a Alert
		var seq *int64
		if err := rows.Scan(&a.ID, &a.SourceChain, &a.ChannelID, &a.Kind, &seq,
			&a.BodyDigest, &a.Detail, &a.CreatedAt); err != nil {
			return nil, err
		}
		a.Sequence = seq
		out = append(out, a)
	}
	return out, rows.Err()
}

// ListEvidence lists conflict records, optionally scoped to one channel.
func (q *Queries) ListEvidence(ctx context.Context, chain, channel string, limit int) ([]Evidence, error) {
	rows, err := q.DB.Query(ctx,
		`SELECT id, source_chain, channel_id, sequence, block_hash, body_digest,
		        existing_status, body
		   FROM message_evidence
		  WHERE ($1='' OR source_chain=$1)
		    AND ($2='' OR channel_id=$2)
		  ORDER BY id DESC LIMIT $3`,
		chain, channel, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Evidence
	for rows.Next() {
		var (
			e    Evidence
			body []byte
		)
		if err := rows.Scan(&e.ID, &e.SourceChain, &e.ChannelID, &e.Sequence,
			&e.BlockHash, &e.BodyDigest, &e.ExistingStatus, &body); err != nil {
			return nil, err
		}
		e.Body = body
		out = append(out, e)
	}
	return out, rows.Err()
}

// Accounts -----------------------------------------------------------------

// GetAccount reads an account balance; ErrNotFound when absent (implicit 0).
func (q *Queries) GetAccount(ctx context.Context, address string) (*Account, error) {
	row := q.DB.QueryRow(ctx,
		`SELECT address, balance FROM accounts WHERE address=$1`, address)
	var a Account
	err := row.Scan(&a.Address, &a.Balance)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	return &a, nil
}

// AdjustBalance applies a signed integer delta atomically. Negative resulting
// balances are rejected by the CHECK constraint (insufficient funds).
func (q *Queries) AdjustBalance(ctx context.Context, address string, delta int64) error {
	_, err := q.DB.Exec(ctx,
		`INSERT INTO accounts(address, balance) VALUES ($1, $2)
		 ON CONFLICT (address) DO UPDATE
		   SET balance = accounts.balance + EXCLUDED.balance,
		       updated_at = now()`,
		address, delta)
	return err
}

// ListAccounts lists all accounts ordered by address.
func (q *Queries) ListAccounts(ctx context.Context) ([]Account, error) {
	rows, err := q.DB.Query(ctx,
		`SELECT address, balance FROM accounts ORDER BY address`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Account
	for rows.Next() {
		var a Account
		if err := rows.Scan(&a.Address, &a.Balance); err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	return out, rows.Err()
}

// Execution ledger ---------------------------------------------------------

// HasExecution reports whether a sequence was already executed (crash probe).
func (q *Queries) HasExecution(ctx context.Context, chain, channel string, seq int64) (bool, error) {
	var exists bool
	err := q.DB.QueryRow(ctx,
		`SELECT EXISTS(SELECT 1 FROM executions
		                WHERE source_chain=$1 AND channel_id=$2 AND sequence=$3)`,
		chain, channel, seq).Scan(&exists)
	return exists, err
}

// ListBlocks lists blocks of a chain ordered by height.
func (q *Queries) ListBlocks(ctx context.Context, chain string, limit int) ([]Block, error) {
	rows, err := q.DB.Query(ctx,
		`SELECT source_chain, height, hash, parent_hash, status
		   FROM blocks WHERE source_chain=$1 ORDER BY height DESC LIMIT $2`,
		chain, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Block
	for rows.Next() {
		b, err := scanBlock(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, *b)
	}
	return out, rows.Err()
}

// Test support -------------------------------------------------------------

// TruncateAll wipes every table (tests only).
func (s *Store) TruncateAll(ctx context.Context) error {
	_, err := s.Pool.Exec(ctx,
		`TRUNCATE TABLE executions, accounts, alerts, message_evidence,
		           messages, blocks, channels RESTART IDENTITY CASCADE`)
	if err != nil {
		return err
	}
	return s.Queries.EnsureChains(ctx, []string{"chainA", "chainB"})
}

// helpers ------------------------------------------------------------------

type rowScanner interface {
	Scan(dest ...any) error
}

func scanBlock(r rowScanner) (*Block, error) {
	var b Block
	if err := r.Scan(&b.SourceChain, &b.Height, &b.Hash, &b.ParentHash, &b.Status); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	return &b, nil
}

func scanMessage(r rowScanner) (*Message, error) {
	var (
		m    Message
		body []byte
	)
	if err := r.Scan(&m.SourceChain, &m.ChannelID, &m.Sequence, &m.BlockHash,
		&m.BodyDigest, &body, &m.Status, &m.FailureReason); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	m.Body = body
	return &m, nil
}

func scanMessageWithBlock(r rowScanner) (*Message, string, error) {
	var (
		m           Message
		body        []byte
		blockStatus string
	)
	if err := r.Scan(&m.SourceChain, &m.ChannelID, &m.Sequence, &m.BlockHash,
		&m.BodyDigest, &body, &m.Status, &m.FailureReason, &blockStatus); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, "", ErrNotFound
		}
		return nil, "", err
	}
	m.Body = body
	return &m, blockStatus, nil
}

func collectMessages(rows pgx.Rows) ([]Message, error) {
	var out []Message
	for rows.Next() {
		m, err := scanMessage(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, *m)
	}
	return out, rows.Err()
}

func nullable(s string) any {
	if s == "" {
		return nil
	}
	return s
}
