package core

import (
	"context"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// querier is the subset of pgx.Tx used by the core so that statements
// compile uniformly inside and outside transactions.
type querier interface {
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// inTx runs fn in a SERIALIZABLE transaction exposed as the minimal querier
// interface the core uses.
func (e *Executor) inTx(ctx context.Context, fn func(q) error) error {
	return e.store.InTx(ctx, func(tx pgx.Tx) error { return fn(tx) })
}

func isNoRows(err error) bool { return err == pgx.ErrNoRows }

// uniqueViolation reports whether err is a PostgreSQL unique_violation (23505).
func uniqueViolation(err error) bool {
	var pgErr *pgconn.PgError
	if asPgx(err, &pgErr) {
		return pgErr.Code == "23505"
	}
	return false
}

func asPgx(err error, target **pgconn.PgError) bool {
	if e, ok := err.(*pgconn.PgError); ok {
		*target = e
		return true
	}
	return false
}

type chainRow struct {
	id            string
	validatorPub  []byte
	confirmations int64
}

func loadChain(ctx context.Context, q querier, chainID string) (chainRow, error) {
	var c chainRow
	err := q.QueryRow(ctx,
		`SELECT id, validator_pub, confirmations FROM chains WHERE id = $1`,
		chainID).Scan(&c.id, &c.validatorPub, &c.confirmations)
	if isNoRows(err) {
		return c, domErr(CodeUnknownChain, "unknown chain %q", chainID)
	}
	return c, err
}
