package pgstore

import (
	"context"
	"database/sql"
	"errors"
	"fmt"

	"forkindexer/internal/domain"
)

// ErrNotFound is returned by single-entity reads.
var ErrNotFound = errors.New("not found")

// ChainView is the JSON-serializable chain state.
type ChainView struct {
	Head      string `json:"head"`
	Height    int64  `json:"height"`
	CumWeight int64  `json:"cum_weight"`
	Cursor    int64  `json:"cursor"`
}

// BlockView describes a stored block together with its chain membership.
type BlockView struct {
	Hash       string            `json:"hash"`
	ParentHash string            `json:"parent_hash"`
	Height     int64             `json:"height"`
	CumWeight  int64             `json:"cum_weight"`
	Status     string            `json:"status"`
	InChain    bool              `json:"in_chain"`
	Transfers  []domain.Transfer `json:"transfers"`
}

// TxView is one transfer together with the block that carries it.
type TxView struct {
	BlockHash string          `json:"block_hash"`
	Index     int             `json:"index"`
	Transfer  domain.Transfer `json:"transfer"`
	InChain   bool            `json:"in_chain"`
}

// Head returns the canonical chain tip and cursor. Empty head means no
// connected chain exists yet (genesis not delivered).
func (s *Store) Head(ctx context.Context) (ChainView, error) {
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{
		Isolation: sql.LevelRepeatableRead,
		ReadOnly:  true,
	})
	if err != nil {
		return ChainView{}, err
	}
	defer tx.Rollback()

	var head sql.NullString
	var cursor int64
	if err := tx.QueryRowContext(ctx,
		"SELECT head, cursor_seq FROM meta WHERE id = 1").Scan(&head, &cursor); err != nil {
		return ChainView{}, err
	}
	v := ChainView{Cursor: cursor}
	if head.Valid && head.String != "" {
		if err := tx.QueryRowContext(ctx,
			"SELECT height, cum_weight FROM blocks WHERE hash = $1", head.String).
			Scan(&v.Height, &v.CumWeight); err != nil {
			return ChainView{}, err
		}
		v.Head = head.String
	}
	return v, tx.Commit()
}

// Balance returns the indexed balance of one address under the canonical
// head. Unknown addresses have balance 0. The read is one snapshot
// transaction so a concurrent reorg cannot mix heads.
func (s *Store) Balance(ctx context.Context, address string) (int64, string, error) {
	addr, err := domain.ValidateAddress(address)
	if err != nil {
		return 0, "", err
	}
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{
		Isolation: sql.LevelRepeatableRead,
		ReadOnly:  true,
	})
	if err != nil {
		return 0, "", err
	}
	defer tx.Rollback()

	var head sql.NullString
	if err := tx.QueryRowContext(ctx, "SELECT head FROM meta WHERE id = 1").Scan(&head); err != nil {
		return 0, "", err
	}
	var amount int64
	switch err := tx.QueryRowContext(ctx,
		"SELECT amount FROM balances WHERE address = $1", addr).Scan(&amount); {
	case errors.Is(err, sql.ErrNoRows):
		amount = 0
	case err != nil:
		return 0, "", err
	}
	if err := tx.Commit(); err != nil {
		return 0, "", err
	}
	return amount, head.String, nil
}

// Block returns one stored block by hash.
func (s *Store) Block(ctx context.Context, hash string) (BlockView, error) {
	h, err := domain.ValidateHash(hash)
	if err != nil {
		return BlockView{}, err
	}
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{
		Isolation: sql.LevelRepeatableRead,
		ReadOnly:  true,
	})
	if err != nil {
		return BlockView{}, err
	}
	defer tx.Rollback()

	v, err := getBlockTx(ctx, tx, h)
	if err != nil {
		return BlockView{}, err
	}
	return v, tx.Commit()
}

func getBlockTx(ctx context.Context, tx *sql.Tx, h string) (BlockView, error) {
	var v BlockView
	err := tx.QueryRowContext(ctx,
		`SELECT hash, parent_hash, height, cum_weight, status, in_chain
		 FROM blocks WHERE hash = $1`, h).
		Scan(&v.Hash, &v.ParentHash, &v.Height, &v.CumWeight, &v.Status, &v.InChain)
	if errors.Is(err, sql.ErrNoRows) {
		return BlockView{}, ErrNotFound
	}
	if err != nil {
		return BlockView{}, err
	}
	v.Transfers = []domain.Transfer{}
	rows, err := tx.QueryContext(ctx,
		`SELECT from_addr, to_addr, amount FROM transfers
		 WHERE block_hash = $1 ORDER BY idx`, h)
	if err != nil {
		return BlockView{}, err
	}
	defer rows.Close()
	for rows.Next() {
		var t domain.Transfer
		if err := rows.Scan(&t.From, &t.To, &t.Amount); err != nil {
			return BlockView{}, err
		}
		v.Transfers = append(v.Transfers, t)
	}
	return v, rows.Err()
}

// BlockTransactions returns the transfers carried by one block.
func (s *Store) BlockTransactions(ctx context.Context, hash string) ([]TxView, error) {
	h, err := domain.ValidateHash(hash)
	if err != nil {
		return nil, err
	}
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{
		Isolation: sql.LevelRepeatableRead,
		ReadOnly:  true,
	})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()

	var exists int
	if err := tx.QueryRowContext(ctx,
		"SELECT 1 FROM blocks WHERE hash = $1", h).Scan(&exists); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	var inChain bool
	if err := tx.QueryRowContext(ctx, "SELECT in_chain FROM blocks WHERE hash=$1", h).
		Scan(&inChain); err != nil {
		return nil, err
	}
	rows, err := tx.QueryContext(ctx,
		`SELECT idx, from_addr, to_addr, amount FROM transfers
		 WHERE block_hash = $1 ORDER BY idx`, h)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []TxView{}
	for rows.Next() {
		var tv TxView
		tv.BlockHash = h
		tv.InChain = inChain
		if err := rows.Scan(&tv.Index, &tv.Transfer.From, &tv.Transfer.To,
			&tv.Transfer.Amount); err != nil {
			return nil, err
		}
		out = append(out, tv)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return out, tx.Commit()
}

// AddressTransactions returns every transfer touching an address on the
// canonical chain, ordered by chain height then block hash then index.
func (s *Store) AddressTransactions(ctx context.Context, address string, limit int) ([]TxView, error) {
	addr, err := domain.ValidateAddress(address)
	if err != nil {
		return nil, err
	}
	if limit <= 0 || limit > 1000 {
		limit = 100
	}
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{
		Isolation: sql.LevelRepeatableRead,
		ReadOnly:  true,
	})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()

	rows, err := tx.QueryContext(ctx, `
SELECT t.block_hash, t.idx, t.from_addr, t.to_addr, t.amount, b.in_chain
FROM transfers t JOIN blocks b ON b.hash = t.block_hash
WHERE b.in_chain AND (t.from_addr = $1 OR t.to_addr = $1)
ORDER BY b.height, t.block_hash, t.idx
LIMIT $2`, addr, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []TxView{}
	for rows.Next() {
		var tv TxView
		if err := rows.Scan(&tv.BlockHash, &tv.Index, &tv.Transfer.From,
			&tv.Transfer.To, &tv.Transfer.Amount, &tv.InChain); err != nil {
			return nil, err
		}
		out = append(out, tv)
	}
	return out, rows.Err()
}

// ChainBlocks returns blocks of the canonical chain from height start upward,
// at most limit blocks (oldest first).
func (s *Store) ChainBlocks(ctx context.Context, startHeight, limit int64) ([]BlockView, error) {
	if limit <= 0 || limit > 500 {
		limit = 100
	}
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{
		Isolation: sql.LevelRepeatableRead,
		ReadOnly:  true,
	})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()

	rows, err := tx.QueryContext(ctx, `
SELECT hash FROM blocks
WHERE in_chain AND height >= $1
ORDER BY height ASC LIMIT $2`, startHeight, limit)
	if err != nil {
		return nil, err
	}
	var hashes []string
	for rows.Next() {
		var h string
		if err := rows.Scan(&h); err != nil {
			rows.Close()
			return nil, err
		}
		hashes = append(hashes, h)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return nil, err
	}
	out := make([]BlockView, 0, len(hashes))
	for _, h := range hashes {
		v, err := getBlockTx(ctx, tx, h)
		if err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, tx.Commit()
}

// VerifyReport is the outcome of rebuilding state from genesis.
type VerifyReport struct {
	OK               bool             `json:"ok"`
	Head             string           `json:"head"`
	Cursor           int64            `json:"cursor"`
	ExpectedBalances map[string]int64 `json:"expected_balances"`
	ActualBalances   map[string]int64 `json:"actual_balances"`
	BalanceMismatch  map[string]struct {
		Expected int64 `json:"expected"`
		Actual   int64 `json:"actual"`
	} `json:"balance_mismatches"`
	ChainMismatch []string `json:"chain_mismatches"`
	Notes         []string `json:"notes,omitempty"`
}

// VerifyFromGenesis rebuilds balances independently from the stored canonical
// chain and compares them with the indexed balances, all in one repeatable
// read snapshot. This is the crash/restart acceptance oracle.
func (s *Store) VerifyFromGenesis(ctx context.Context) (VerifyReport, error) {
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{
		Isolation: sql.LevelRepeatableRead,
		ReadOnly:  true,
	})
	if err != nil {
		return VerifyReport{}, err
	}
	defer tx.Rollback()

	rep := VerifyReport{
		OK:               true,
		ExpectedBalances: map[string]int64{},
		ActualBalances:   map[string]int64{},
		BalanceMismatch: map[string]struct {
			Expected int64 `json:"expected"`
			Actual   int64 `json:"actual"`
		}{},
	}

	var head sql.NullString
	if err := tx.QueryRowContext(ctx,
		"SELECT head, cursor_seq FROM meta WHERE id = 1").Scan(&head, &rep.Cursor); err != nil {
		return VerifyReport{}, err
	}
	if head.Valid {
		rep.Head = head.String
	}

	// Independently walk the canonical chain tip->genesis and replay in
	// genesis->tip order.
	chainHashes, err := ancestryTx(ctx, tx, rep.Head)
	if err != nil {
		return VerifyReport{}, err
	}
	for i := len(chainHashes) - 1; i >= 0; i-- {
		h := chainHashes[i]
		var inChain bool
		var parent string
		var height int64
		if err := tx.QueryRowContext(ctx,
			"SELECT in_chain, parent_hash, height FROM blocks WHERE hash=$1", h).
			Scan(&inChain, &parent, &height); err != nil {
			return VerifyReport{}, err
		}
		if !inChain {
			rep.OK = false
			rep.ChainMismatch = append(rep.ChainMismatch,
				fmt.Sprintf("%s belongs to ancestry but in_chain=false", h))
		}
		rows, err := tx.QueryContext(ctx,
			`SELECT from_addr, to_addr, amount FROM transfers
			 WHERE block_hash=$1 ORDER BY idx`, h)
		if err != nil {
			return VerifyReport{}, err
		}
		var ts []domain.Transfer
		for rows.Next() {
			var t domain.Transfer
			if err := rows.Scan(&t.From, &t.To, &t.Amount); err != nil {
				rows.Close()
				return VerifyReport{}, err
			}
			ts = append(ts, t)
		}
		rows.Close()
		if err := rows.Err(); err != nil {
			return VerifyReport{}, err
		}
		for _, t := range ts {
			if err := domain.ApplyTransfer(rep.ExpectedBalances, t); err != nil {
				return VerifyReport{}, fmt.Errorf("rebuild overflow at block %s: %w", h, err)
			}
		}
	}

	// Cross-check: every block marked in_chain must be exactly the ancestry.
	rows, err := tx.QueryContext(ctx, "SELECT hash FROM blocks WHERE in_chain ORDER BY height")
	if err != nil {
		return VerifyReport{}, err
	}
	marked := map[string]bool{}
	for rows.Next() {
		var h string
		if err := rows.Scan(&h); err != nil {
			rows.Close()
			return VerifyReport{}, err
		}
		marked[h] = true
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return VerifyReport{}, err
	}
	anc := map[string]bool{}
	for _, h := range chainHashes {
		anc[h] = true
	}
	for h := range marked {
		if !anc[h] {
			rep.OK = false
			rep.ChainMismatch = append(rep.ChainMismatch,
				fmt.Sprintf("%s in_chain=true but not under head", h))
		}
	}
	for _, h := range chainHashes {
		if !marked[h] {
			rep.OK = false
			rep.ChainMismatch = append(rep.ChainMismatch,
				fmt.Sprintf("%s under head but in_chain=false", h))
		}
	}

	// Read the entire indexed balance table.
	rows, err = tx.QueryContext(ctx, "SELECT address, amount FROM balances")
	if err != nil {
		return VerifyReport{}, err
	}
	for rows.Next() {
		var a string
		var n int64
		if err := rows.Scan(&a, &n); err != nil {
			rows.Close()
			return VerifyReport{}, err
		}
		rep.ActualBalances[a] = n
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return VerifyReport{}, err
	}

	all := map[string]bool{}
	for a := range rep.ExpectedBalances {
		all[a] = true
	}
	for a := range rep.ActualBalances {
		all[a] = true
	}
	for a := range all {
		exp := rep.ExpectedBalances[a]
		act := rep.ActualBalances[a]
		if exp != act {
			rep.OK = false
			rep.BalanceMismatch[a] = struct {
				Expected int64 `json:"expected"`
				Actual   int64 `json:"actual"`
			}{Expected: exp, Actual: act}
		}
	}

	// Every delivered sequence must exist and be contiguous up to the cursor.
	var cnt int64
	if err := tx.QueryRowContext(ctx,
		"SELECT count(*) FROM deliveries WHERE sequence BETWEEN 1 AND $1", rep.Cursor).
		Scan(&cnt); err != nil {
		return VerifyReport{}, err
	}
	if cnt != rep.Cursor {
		rep.OK = false
		rep.Notes = append(rep.Notes,
			fmt.Sprintf("delivery rows %d != cursor %d", cnt, rep.Cursor))
	}

	if err := tx.Commit(); err != nil {
		return VerifyReport{}, err
	}
	return rep, nil
}

func ancestryTx(ctx context.Context, tx *sql.Tx, tip string) ([]string, error) {
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
